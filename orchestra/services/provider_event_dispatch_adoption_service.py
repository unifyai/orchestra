"""Orchestra-authoritative adoption claims for provider-event dispatch launches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import ProviderEventDispatch
from orchestra.provider_triggers.runtime_types import (
    DispatchProcessingState,
    DownstreamAdoptionStatus,
)
from orchestra.settings import settings

TERMINAL_ADOPTION_STATUSES = {
    DownstreamAdoptionStatus.started.value,
    DownstreamAdoptionStatus.terminal.value,
}
PUBLIC_ADOPTION_STATUSES = {
    DownstreamAdoptionStatus.adopted.value,
    DownstreamAdoptionStatus.started.value,
    DownstreamAdoptionStatus.terminal.value,
}


class DispatchAdoptionError(Exception):
    """Stable failure while claiming or reporting dispatch adoption."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class DispatchAuthorizationSnapshot:
    """Immutable authorization fields that must match the dispatch row."""

    operation_id: str
    run_id: int
    run_key: str
    assistant_id: int
    task_id: int
    binding_id: str
    receipt_id: str
    accepted_revision: str
    dispatch_mode: str
    audience: str


@dataclass(frozen=True)
class DispatchAdoptionClaimResult:
    """Result of one compare-and-set adoption claim."""

    operation_id: str
    run_id: int
    run_key: str
    status: str
    fencing_token: int
    owns_launch: bool
    launch_identity: str | None
    terminal_reason: str | None


@dataclass(frozen=True)
class DispatchAdoptionReportResult:
    """Result of one fenced start or terminal report."""

    operation_id: str
    run_id: int
    run_key: str
    status: str
    fencing_token: int
    launch_identity: str | None
    terminal_reason: str | None


class ProviderEventDispatchAdoptionService:
    """Own cross-instance launch adoption for one immutable dispatch operation."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._dao = ProviderTriggerDAO(session)

    def claim(
        self,
        *,
        authorization: DispatchAuthorizationSnapshot,
        claimant_id: str,
        launch_identity: str | None = None,
        lease_ttl: timedelta | None = None,
    ) -> DispatchAdoptionClaimResult:
        """Claim launch ownership or return the durable recorded adoption state."""

        if not claimant_id:
            raise DispatchAdoptionError("claimant_id_required")

        dispatch = self._dao.get_dispatch_by_operation_id(
            operation_id=authorization.operation_id,
            for_update=True,
        )
        if dispatch is None:
            raise DispatchAdoptionError("provider_event_dispatch_not_found")

        self._assert_authorization_matches(dispatch, authorization)

        public_status = self._public_status(dispatch)
        if public_status in TERMINAL_ADOPTION_STATUSES:
            return self._claim_result(
                dispatch,
                owns_launch=False,
                status=public_status,
            )

        now = datetime.now(timezone.utc)
        lease_owner = dispatch.downstream_adoption_lease_owner
        lease_expires_at = dispatch.downstream_adoption_lease_expires_at
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)

        ttl = lease_ttl or timedelta(
            seconds=settings.provider_trigger_adoption_lease_seconds,
        )
        lease_active = (
            lease_owner is not None
            and lease_expires_at is not None
            and lease_expires_at > now
        )
        if lease_active and lease_owner != claimant_id:
            return self._claim_result(
                dispatch,
                owns_launch=False,
                status=DownstreamAdoptionStatus.adopted.value,
            )

        if lease_active and lease_owner == claimant_id:
            # Same rail process retrying before lease expiry keeps its fence.
            dispatch.downstream_adoption_lease_expires_at = now + ttl
            dispatch.downstream_adoption_status = DownstreamAdoptionStatus.adopted.value
            dispatch.downstream_status_at = now
            if launch_identity and not dispatch.downstream_adoption_ref:
                dispatch.downstream_adoption_ref = launch_identity
            self._session.flush()
            return self._claim_result(dispatch, owns_launch=True)

        dispatch.downstream_adoption_lease_owner = claimant_id
        dispatch.downstream_adoption_lease_expires_at = now + ttl
        dispatch.downstream_adoption_fencing_token = (
            int(dispatch.downstream_adoption_fencing_token or 0) + 1
        )
        dispatch.downstream_adoption_status = DownstreamAdoptionStatus.adopted.value
        dispatch.downstream_status_at = now
        if launch_identity:
            dispatch.downstream_adoption_ref = launch_identity
        if dispatch.processing_state in {
            DispatchProcessingState.pending.value,
            DispatchProcessingState.claimed.value,
            DispatchProcessingState.retryable.value,
        }:
            # Rail adoption can outpace worker delivery acknowledgement.
            dispatch.processing_state = DispatchProcessingState.delivered.value
            if dispatch.delivered_at is None:
                dispatch.delivered_at = now

        self._session.flush()
        return self._claim_result(dispatch, owns_launch=True)

    def report_started(
        self,
        *,
        operation_id: str,
        fencing_token: int,
        launch_identity: str | None = None,
    ) -> DispatchAdoptionReportResult:
        """Record fenced start for the current adoption owner."""

        dispatch = self._require_fenced_dispatch(
            operation_id=operation_id,
            fencing_token=fencing_token,
            allow_started=True,
        )
        if self._public_status(dispatch) == DownstreamAdoptionStatus.started.value:
            if launch_identity and not dispatch.downstream_adoption_ref:
                dispatch.downstream_adoption_ref = launch_identity
                self._session.flush()
            return self._report_result(dispatch)

        now = datetime.now(timezone.utc)
        if launch_identity:
            dispatch.downstream_adoption_ref = launch_identity
        self._dao.mark_dispatch_started(
            dispatch=dispatch,
            adoption_status=DownstreamAdoptionStatus.started.value,
            adoption_ref=dispatch.downstream_adoption_ref,
        )
        dispatch.downstream_adoption_lease_owner = None
        dispatch.downstream_adoption_lease_expires_at = None
        dispatch.downstream_status_at = now
        self._session.flush()
        return self._report_result(dispatch)

    def report_terminal(
        self,
        *,
        operation_id: str,
        fencing_token: int,
        terminal_reason: str,
        launch_identity: str | None = None,
    ) -> DispatchAdoptionReportResult:
        """Record fenced terminal failure for the current adoption owner."""

        if not terminal_reason:
            raise DispatchAdoptionError("terminal_reason_required")

        dispatch = self._require_fenced_dispatch(
            operation_id=operation_id,
            fencing_token=fencing_token,
            allow_terminal=True,
        )
        if self._public_status(dispatch) == DownstreamAdoptionStatus.terminal.value:
            return self._report_result(dispatch)

        if launch_identity:
            dispatch.downstream_adoption_ref = launch_identity
        self._dao.mark_dispatch_failed(
            dispatch=dispatch,
            error_code=terminal_reason,
            adoption_status=DownstreamAdoptionStatus.terminal.value,
            adoption_ref=dispatch.downstream_adoption_ref or terminal_reason,
        )
        dispatch.downstream_adoption_lease_owner = None
        dispatch.downstream_adoption_lease_expires_at = None
        self._session.flush()
        return self._report_result(dispatch)

    def get_public_status(
        self,
        *,
        operation_id: str,
    ) -> DispatchAdoptionReportResult:
        """Return Orchestra's durable public adoption status for one operation."""

        dispatch = self._dao.get_dispatch_by_operation_id(operation_id=operation_id)
        if dispatch is None:
            raise DispatchAdoptionError("provider_event_dispatch_not_found")
        return self._report_result(dispatch)

    def _require_fenced_dispatch(
        self,
        *,
        operation_id: str,
        fencing_token: int,
        allow_started: bool = False,
        allow_terminal: bool = False,
    ) -> ProviderEventDispatch:
        dispatch = self._dao.get_dispatch_by_operation_id(
            operation_id=operation_id,
            for_update=True,
        )
        if dispatch is None:
            raise DispatchAdoptionError("provider_event_dispatch_not_found")
        if int(dispatch.downstream_adoption_fencing_token or 0) != int(fencing_token):
            raise DispatchAdoptionError("fencing_token_mismatch")
        public_status = self._public_status(dispatch)
        if (
            public_status == DownstreamAdoptionStatus.terminal.value
            and not allow_terminal
        ):
            raise DispatchAdoptionError("dispatch_already_terminal")
        if (
            public_status == DownstreamAdoptionStatus.started.value
            and not allow_started
            and not allow_terminal
        ):
            raise DispatchAdoptionError("dispatch_already_started")
        return dispatch

    @staticmethod
    def _assert_authorization_matches(
        dispatch: ProviderEventDispatch,
        authorization: DispatchAuthorizationSnapshot,
    ) -> None:
        expected = (
            ("run_id", int(dispatch.run_id), int(authorization.run_id)),
            ("run_key", str(dispatch.run_key), str(authorization.run_key)),
            (
                "assistant_id",
                int(dispatch.assistant_id),
                int(authorization.assistant_id),
            ),
            ("task_id", int(dispatch.task_id), int(authorization.task_id)),
            ("binding_id", str(dispatch.binding_id), str(authorization.binding_id)),
            ("receipt_id", str(dispatch.receipt_id), str(authorization.receipt_id)),
            (
                "accepted_revision",
                str(dispatch.accepted_activation_revision),
                str(authorization.accepted_revision),
            ),
            (
                "dispatch_mode",
                str(dispatch.dispatch_mode),
                str(authorization.dispatch_mode),
            ),
            ("audience", str(dispatch.audience), str(authorization.audience)),
        )
        for _field, left, right in expected:
            if left != right:
                raise DispatchAdoptionError("dispatch_authorization_mismatch")

    @staticmethod
    def _public_status(dispatch: ProviderEventDispatch) -> str:
        status = dispatch.downstream_adoption_status
        if status in PUBLIC_ADOPTION_STATUSES:
            return str(status)
        if dispatch.processing_state == DispatchProcessingState.started.value:
            return DownstreamAdoptionStatus.started.value
        if dispatch.processing_state in {
            DispatchProcessingState.succeeded.value,
            DispatchProcessingState.failed.value,
        }:
            return DownstreamAdoptionStatus.terminal.value
        if status == DownstreamAdoptionStatus.published.value:
            return DownstreamAdoptionStatus.adopted.value
        if status:
            return DownstreamAdoptionStatus.adopted.value
        return DownstreamAdoptionStatus.adopted.value

    def _claim_result(
        self,
        dispatch: ProviderEventDispatch,
        *,
        owns_launch: bool,
        status: str | None = None,
    ) -> DispatchAdoptionClaimResult:
        return DispatchAdoptionClaimResult(
            operation_id=dispatch.operation_id,
            run_id=dispatch.run_id,
            run_key=dispatch.run_key,
            status=status or self._public_status(dispatch),
            fencing_token=int(dispatch.downstream_adoption_fencing_token or 0),
            owns_launch=owns_launch,
            launch_identity=dispatch.downstream_adoption_ref,
            terminal_reason=dispatch.terminal_error_code,
        )

    def _report_result(
        self,
        dispatch: ProviderEventDispatch,
    ) -> DispatchAdoptionReportResult:
        return DispatchAdoptionReportResult(
            operation_id=dispatch.operation_id,
            run_id=dispatch.run_id,
            run_key=dispatch.run_key,
            status=self._public_status(dispatch),
            fencing_token=int(dispatch.downstream_adoption_fencing_token or 0),
            launch_identity=dispatch.downstream_adoption_ref,
            terminal_reason=dispatch.terminal_error_code,
        )
