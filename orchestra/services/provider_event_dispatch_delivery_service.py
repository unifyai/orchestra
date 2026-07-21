"""Deliver provider-event dispatch operations and converge downstream status."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import ProviderEventDispatch
from orchestra.provider_triggers.dispatch_request import (
    COMMUNICATION_DISPATCH_AUDIENCE,
    UNITY_DISPATCH_AUDIENCE,
    ProviderEventDispatchRequest,
)
from orchestra.provider_triggers.runtime_types import (
    DispatchErrorCode,
    DispatchProcessingState,
    DownstreamAdoptionStatus,
)
from orchestra.services.task_machine_state_service import get_task_run_by_run_id
from orchestra.settings import settings

logger = logging.getLogger(__name__)

COMM_PROVIDER_EVENT_DISPATCH_PATH = "/infra/task-execution/provider-event-dispatch"
ADAPTERS_SYSTEM_EVENT_PATH = "/unity/system-event"

TERMINAL_RUN_STATES = {"completed", "succeeded", "failed", "cancelled"}
ACTIVE_RUN_STATES = {"running", "active"}
QUEUED_RUN_STATES = {"pending", "queued"}


class ProviderEventDispatchDeliveryService:
    """Claim, deliver, and converge provider-event dispatch operations."""

    def __init__(
        self,
        session: Session,
        *,
        lease_owner: str | None = None,
        http_client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._session = session
        self._dao = ProviderTriggerDAO(session)
        self._lease_owner = lease_owner or f"trigger-worker-{uuid.uuid4().hex[:8]}"
        self._http_client_factory = http_client_factory or (
            lambda: httpx.Client(
                timeout=settings.provider_trigger_dispatch_http_timeout_seconds,
            )
        )

    @property
    def lease_owner(self) -> str:
        return self._lease_owner

    def process_dispatch_batch(
        self,
        *,
        batch_size: int | None = None,
    ) -> dict[str, int]:
        """Claim and deliver one batch of pending dispatch operations."""

        dispatches = self._dao.claim_dispatches_for_delivery(
            lease_owner=self._lease_owner,
            limit=batch_size or settings.provider_trigger_dispatch_batch_size,
            lease_ttl=timedelta(
                seconds=settings.provider_trigger_dispatch_lease_seconds,
            ),
        )
        if dispatches:
            self._session.commit()

        stats = {
            "dispatches_claimed": len(dispatches),
            "dispatches_delivered": 0,
            "dispatches_started": 0,
            "dispatches_retryable": 0,
            "dispatches_failed": 0,
            "dispatches_duplicate_prevented": 0,
        }
        with self._http_client_factory() as client:
            for dispatch in dispatches:
                try:
                    outcome = self._deliver_dispatch(dispatch, client=client)
                    stats[outcome] += 1
                except Exception:
                    logger.exception(
                        "provider-event dispatch delivery failed operation_id=%s",
                        dispatch.operation_id,
                    )
                    self._dao.mark_dispatch_retryable(
                        dispatch=dispatch,
                        error_code=DispatchErrorCode.dispatch_transport_failed.value,
                    )
                    stats["dispatches_retryable"] += 1
                self._session.commit()
        return stats

    def process_status_convergence_batch(
        self,
        *,
        batch_size: int | None = None,
    ) -> dict[str, int]:
        """Poll and converge one batch of in-flight dispatch operations."""

        dispatches = self._dao.list_dispatches_for_status_convergence(
            limit=batch_size or settings.provider_trigger_dispatch_status_batch_size,
        )
        stats = {
            "dispatches_converged": 0,
            "dispatches_still_in_flight": 0,
            "dispatches_terminal_failed": 0,
            "dispatches_terminal_succeeded": 0,
        }
        with self._http_client_factory() as client:
            for dispatch in dispatches:
                try:
                    outcome = self._converge_dispatch(dispatch, client=client)
                    stats[outcome] += 1
                except Exception:
                    logger.exception(
                        "provider-event dispatch convergence failed operation_id=%s",
                        dispatch.operation_id,
                    )
                    self._schedule_next_poll(dispatch)
                    stats["dispatches_still_in_flight"] += 1
                self._session.commit()
        return stats

    def backlog_oldest_age_seconds(self) -> int | None:
        """Return the age in seconds of the oldest non-terminal dispatch."""

        backlog = self._dao.list_dispatch_backlog(limit=1)
        if not backlog:
            return None
        oldest = backlog[0]
        created_at = oldest.created_at
        if created_at is None:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
        return max(0, int(age.total_seconds()))

    def _deliver_dispatch(
        self,
        dispatch: ProviderEventDispatch,
        *,
        client: httpx.Client,
    ) -> str:
        if not dispatch.event_context_ref:
            self._dao.mark_dispatch_failed(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_validation_failed.value,
            )
            return "dispatches_failed"

        request = self._build_dispatch_request(dispatch)
        if dispatch.dispatch_mode == "offline":
            return self._deliver_offline(dispatch, request, client=client)
        return self._deliver_live(dispatch, request, client=client)

    def _deliver_offline(
        self,
        dispatch: ProviderEventDispatch,
        request: ProviderEventDispatchRequest,
        *,
        client: httpx.Client,
    ) -> str:
        comms_url, admin_key = self._rail_credentials()
        if not comms_url or not admin_key:
            self._dao.mark_dispatch_retryable(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_rail_unconfigured.value,
            )
            return "dispatches_retryable"

        url = f"{comms_url}{COMM_PROVIDER_EVENT_DISPATCH_PATH}"
        response = client.post(
            url,
            headers=self._auth_headers(admin_key),
            json=request.model_dump(mode="json"),
        )

        if response.status_code >= 500:
            self._dao.mark_dispatch_retryable(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_transport_failed.value,
            )
            return "dispatches_retryable"

        if response.status_code == 409:
            self._dao.mark_dispatch_failed(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_inbox_mismatch.value,
                adoption_status=DownstreamAdoptionStatus.terminal.value,
            )
            return "dispatches_failed"

        if response.status_code >= 400:
            self._dao.mark_dispatch_failed(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_validation_failed.value,
            )
            return "dispatches_failed"

        payload = response.json()
        return self._apply_public_status(
            dispatch=dispatch,
            status=str(payload.get("status") or ""),
            adoption_ref=payload.get("job_name"),
            adopted_only=bool(payload.get("adopted_only")),
        )

    def _deliver_live(
        self,
        dispatch: ProviderEventDispatch,
        request: ProviderEventDispatchRequest,
        *,
        client: httpx.Client,
    ) -> str:
        adapters_url, admin_key = self._adapters_credentials()
        if not adapters_url or not admin_key:
            self._dao.mark_dispatch_retryable(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_rail_unconfigured.value,
            )
            return "dispatches_retryable"

        url = f"{adapters_url}{ADAPTERS_SYSTEM_EVENT_PATH}"
        body = {
            "assistant_id": str(dispatch.assistant_id),
            "event_type": "provider_event_dispatch",
            "message": (
                f"Provider event dispatch for operation {dispatch.operation_id}"
            ),
            "extra_event_fields": request.model_dump(mode="json"),
        }
        response = client.post(
            url,
            headers=self._auth_headers(admin_key),
            json=body,
        )

        if response.status_code >= 400:
            if response.status_code >= 500:
                self._dao.mark_dispatch_retryable(
                    dispatch=dispatch,
                    error_code=DispatchErrorCode.dispatch_transport_failed.value,
                )
                return "dispatches_retryable"
            self._dao.mark_dispatch_failed(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_downstream_rejected.value,
            )
            return "dispatches_failed"

        self._dao.mark_dispatch_delivered(
            dispatch=dispatch,
            adoption_status=DownstreamAdoptionStatus.published.value,
            adoption_ref=str(dispatch.operation_id),
        )
        return "dispatches_delivered"

    def _converge_dispatch(
        self,
        dispatch: ProviderEventDispatch,
        *,
        client: httpx.Client,
    ) -> str:
        # Adoption/start/terminal are owned by Orchestra claim/report APIs.
        # Convergence observes the pre-created run and already-recorded adoption.
        if (
            dispatch.downstream_adoption_status
            == DownstreamAdoptionStatus.started.value
        ):
            if dispatch.processing_state != DispatchProcessingState.started.value:
                self._dao.mark_dispatch_started(
                    dispatch=dispatch,
                    adoption_status=DownstreamAdoptionStatus.started.value,
                    adoption_ref=dispatch.downstream_adoption_ref,
                )
                return "dispatches_converged"
        if (
            dispatch.downstream_adoption_status
            == DownstreamAdoptionStatus.terminal.value
        ):
            if dispatch.processing_state not in {
                DispatchProcessingState.succeeded.value,
                DispatchProcessingState.failed.value,
            }:
                self._dao.mark_dispatch_failed(
                    dispatch=dispatch,
                    error_code=dispatch.terminal_error_code
                    or DispatchErrorCode.dispatch_downstream_rejected.value,
                    adoption_status=DownstreamAdoptionStatus.terminal.value,
                    adoption_ref=dispatch.downstream_adoption_ref,
                )
                return "dispatches_terminal_failed"

        run_outcome = self._converge_run_state(dispatch)
        if run_outcome is not None:
            return run_outcome

        self._schedule_next_poll(dispatch)
        return "dispatches_still_in_flight"

    def _converge_run_state(self, dispatch: ProviderEventDispatch) -> str | None:
        binding = self._dao.get_binding(binding_id=dispatch.binding_id)
        if binding is None:
            self._schedule_next_poll(dispatch)
            return "dispatches_still_in_flight"

        run_row = get_task_run_by_run_id(
            self._session,
            binding.project_id,
            run_id=dispatch.run_id,
        )
        if run_row is None:
            self._schedule_next_poll(dispatch)
            return "dispatches_still_in_flight"

        run_state = str((run_row.data or {}).get("state") or "").lower()
        if run_state in TERMINAL_RUN_STATES:
            if run_state in {"failed", "cancelled"}:
                error_code = str(
                    (run_row.data or {}).get("terminal_reason")
                    or DispatchErrorCode.dispatch_run_terminal_failed.value,
                )
                self._dao.mark_dispatch_failed(
                    dispatch=dispatch,
                    error_code=error_code,
                    adoption_status=DownstreamAdoptionStatus.terminal.value,
                    adoption_ref=run_state,
                )
                return "dispatches_terminal_failed"
            self._dao.mark_dispatch_succeeded(
                dispatch=dispatch,
                adoption_status=DownstreamAdoptionStatus.terminal.value,
                adoption_ref=run_state,
            )
            return "dispatches_terminal_succeeded"

        if run_state in ACTIVE_RUN_STATES:
            if dispatch.processing_state != DispatchProcessingState.started.value:
                self._dao.mark_dispatch_started(
                    dispatch=dispatch,
                    adoption_status=DownstreamAdoptionStatus.started.value,
                    adoption_ref=run_state,
                )
            else:
                self._schedule_next_poll(dispatch)
            return "dispatches_converged"

        if run_state in QUEUED_RUN_STATES:
            if dispatch.processing_state == DispatchProcessingState.delivered.value:
                self._schedule_next_poll(dispatch)
                return "dispatches_still_in_flight"

        self._schedule_next_poll(dispatch)
        return "dispatches_still_in_flight"

    def _apply_public_status(
        self,
        *,
        dispatch: ProviderEventDispatch,
        status: str,
        adoption_ref: Any = None,
        adopted_only: bool = False,
    ) -> str:
        if status == DownstreamAdoptionStatus.terminal.value:
            self._dao.mark_dispatch_failed(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_downstream_rejected.value,
                adoption_status=status,
                adoption_ref=str(adoption_ref) if adoption_ref else None,
            )
            return "dispatches_failed"

        if status == DownstreamAdoptionStatus.started.value:
            if adopted_only:
                if dispatch.processing_state == DispatchProcessingState.started.value:
                    return "dispatches_duplicate_prevented"
                self._dao.mark_dispatch_started(
                    dispatch=dispatch,
                    adoption_status=status,
                    adoption_ref=str(adoption_ref) if adoption_ref else None,
                )
                return "dispatches_duplicate_prevented"
            self._dao.mark_dispatch_started(
                dispatch=dispatch,
                adoption_status=status,
                adoption_ref=str(adoption_ref) if adoption_ref else None,
            )
            return "dispatches_started"

        if adopted_only and dispatch.processing_state in {
            DispatchProcessingState.delivered.value,
            DispatchProcessingState.started.value,
        }:
            return "dispatches_duplicate_prevented"

        self._dao.mark_dispatch_delivered(
            dispatch=dispatch,
            adoption_status=status or DownstreamAdoptionStatus.adopted.value,
            adoption_ref=str(adoption_ref) if adoption_ref else None,
        )
        return "dispatches_delivered"

    def _build_dispatch_request(
        self,
        dispatch: ProviderEventDispatch,
    ) -> ProviderEventDispatchRequest:
        audience = dispatch.audience
        if dispatch.dispatch_mode == "offline":
            audience = COMMUNICATION_DISPATCH_AUDIENCE
        elif dispatch.dispatch_mode == "live":
            audience = UNITY_DISPATCH_AUDIENCE

        return ProviderEventDispatchRequest(
            operation_id=dispatch.operation_id,
            run_id=dispatch.run_id,
            run_key=dispatch.run_key,
            assistant_id=str(dispatch.assistant_id),
            task_id=dispatch.task_id,
            binding_id=dispatch.binding_id,
            receipt_id=dispatch.receipt_id,
            accepted_revision=dispatch.accepted_activation_revision,
            dispatch_mode=dispatch.dispatch_mode,  # type: ignore[arg-type]
            event_context_ref=dispatch.event_context_ref or "",
            issued_at=datetime.now(timezone.utc),
            audience=audience,
        )

    def _schedule_next_poll(self, dispatch: ProviderEventDispatch) -> None:
        dispatch.next_retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.provider_trigger_dispatch_poll_interval_seconds,
        )
        self._session.flush()

    @staticmethod
    def _auth_headers(admin_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _rail_credentials() -> tuple[str, str]:
        comms_url = os.environ.get("UNITY_COMMS_URL", "").rstrip("/")
        admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
        return comms_url, admin_key

    @staticmethod
    def _adapters_credentials() -> tuple[str, str]:
        adapters_url = os.environ.get("UNITY_ADAPTERS_URL", "").rstrip("/")
        comms_url = os.environ.get("UNITY_COMMS_URL", "").rstrip("/")
        resolved = adapters_url or comms_url
        admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
        return resolved, admin_key
