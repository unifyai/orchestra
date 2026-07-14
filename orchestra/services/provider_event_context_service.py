"""Authorization and lifecycle for matched provider-event context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import ProviderEventReceipt
from orchestra.provider_triggers.dispatch_request import EVENT_CONTEXT_AUDIENCE
from orchestra.provider_triggers.event_context_errors import (
    EventContextAccessError,
    EventContextErrorReason,
)
from orchestra.provider_triggers.event_context_retention import (
    resolve_event_context_expires_at,
)
from orchestra.provider_triggers.private_event_storage import (
    EventBlobAuthenticationError,
)
from orchestra.provider_triggers.runtime_types import (
    BlobAuditAction,
    EventContextUnavailableReason,
)
from orchestra.services.provider_event_blob_service import ProviderEventBlobService
from orchestra.services.task_machine_state_service import (
    get_task_run_by_run_id,
    update_task_run,
)
from orchestra.settings import settings


@dataclass(frozen=True)
class ResolvedEventContext:
    """One authorized provider-event context bundle."""

    receipt_id: str
    run_id: int
    event_context_ref: str
    envelope: dict
    curated_projection: dict
    source_body: object
    expires_at: datetime | None


class ProviderEventContextService:
    """Owns provider-event context authorization, reads, export, and deletion."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._dao = ProviderTriggerDAO(session)
        self._blob_service = ProviderEventBlobService(session)

    def read_for_service(
        self,
        *,
        project_id: int,
        assistant_id: int,
        task_id: int,
        run_id: int,
        receipt_id: str,
        event_context_ref: str,
        audience: str,
        issued_at: datetime,
        actor: str,
    ) -> ResolvedEventContext:
        """Decrypt one context bundle for a Unity service callback."""

        self._validate_service_request(
            audience=audience,
            issued_at=issued_at,
        )
        _, run_data = self._load_owned_run(
            project_id=project_id,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
        )
        run_receipt_id = run_data.get("provider_event_receipt_id")
        if run_receipt_id is None or str(run_receipt_id) != receipt_id:
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt_id,
                reason="run_receipt_mismatch",
                audience=audience,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        receipt = self._authorize_receipt(
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            receipt=self._require_receipt(receipt_id),
            event_context_ref=event_context_ref,
            actor=actor,
            audience=audience,
            audit_action=BlobAuditAction.read_denied,
        )
        return self._decrypt_bundle(
            receipt=receipt,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=actor,
            audience=audience,
            audit_action=BlobAuditAction.read,
        )

    def read_for_user(
        self,
        *,
        project_id: int,
        assistant_id: int,
        task_id: int,
        run_id: int,
        actor: str,
    ) -> ResolvedEventContext:
        """Decrypt one context bundle for an owned user inspection."""

        receipt = self._authorize_user_run(
            project_id=project_id,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=actor,
            audit_action=BlobAuditAction.read_denied,
        )
        return self._decrypt_bundle(
            receipt=receipt,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=actor,
            audience=None,
            audit_action=BlobAuditAction.read,
        )

    def export_for_user(
        self,
        *,
        project_id: int,
        assistant_id: int,
        task_id: int,
        run_id: int,
        actor: str,
    ) -> ResolvedEventContext:
        """Decrypt and audit-export one owned context bundle."""

        receipt = self._authorize_user_run(
            project_id=project_id,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=actor,
            audit_action=BlobAuditAction.read_denied,
        )
        return self._decrypt_bundle(
            receipt=receipt,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=actor,
            audience=None,
            audit_action=BlobAuditAction.export,
        )

    def delete_for_user(
        self,
        *,
        project_id: int,
        assistant_id: int,
        task_id: int,
        run_id: int,
        actor: str,
    ) -> None:
        """Make one owned context unreadable immediately and queue ciphertext deletion."""

        receipt = self._authorize_user_run(
            project_id=project_id,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=actor,
            audit_action=BlobAuditAction.read_denied,
            allow_missing_context=True,
            allow_already_removed=True,
        )
        if receipt.event_context_unavailable_reason is not None:
            return
        self._blob_service.mark_event_context_unavailable(
            receipt=receipt,
            actor=actor,
            reason=EventContextUnavailableReason.deleted.value,
        )

    def stamp_expiry_on_acceptance(
        self,
        *,
        receipt: ProviderEventReceipt,
        run_key: str,
        project_id: int,
        assistant_id: int,
        source_task_log_id: int | None,
    ) -> datetime:
        """Persist retention expiry on a newly accepted receipt and its run."""

        if receipt.event_context_expires_at is None:
            receipt.event_context_expires_at = resolve_event_context_expires_at()
            self._session.flush()
        update_task_run(
            self._session,
            project_id,
            str(assistant_id),
            run_key,
            {
                "event_context_expires_at": receipt.event_context_expires_at.isoformat(),
            },
            source_task_log_id=source_task_log_id,
        )
        return receipt.event_context_expires_at

    def sweep_expired_contexts(self) -> int:
        """Mark due receipts unavailable and queue ciphertext deletion."""

        now = datetime.now(timezone.utc)
        batch_size = settings.trigger_event_context_expiry_batch_size
        due = (
            select(ProviderEventReceipt)
            .where(
                ProviderEventReceipt.event_context_expires_at.isnot(None),
                ProviderEventReceipt.event_context_expires_at <= now,
                or_(
                    ProviderEventReceipt.event_context_ref.isnot(None),
                    ProviderEventReceipt.stable_envelope_json.isnot(None),
                ),
            )
            .order_by(ProviderEventReceipt.id.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        receipts = list(self._session.execute(due).scalars())
        processed = 0
        for receipt in receipts:
            self._blob_service.mark_event_context_unavailable(
                receipt=receipt,
                actor="system",
                reason=EventContextUnavailableReason.expired.value,
            )
            processed += 1
        if processed:
            self._session.flush()
        return processed

    def _validate_service_request(
        self,
        *,
        audience: str,
        issued_at: datetime,
    ) -> None:
        if audience != EVENT_CONTEXT_AUDIENCE:
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        ttl = timedelta(seconds=settings.trigger_event_context_request_ttl_seconds)
        if now - issued_at > ttl or issued_at > now + timedelta(seconds=30):
            raise EventContextAccessError(EventContextErrorReason.token_expired)

    def _authorize_user_run(
        self,
        *,
        project_id: int,
        assistant_id: int,
        task_id: int,
        run_id: int,
        actor: str,
        audit_action: BlobAuditAction,
        allow_missing_context: bool = False,
        allow_already_removed: bool = False,
    ) -> ProviderEventReceipt:
        run, run_data = self._load_owned_run(
            project_id=project_id,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
        )
        receipt_id = run_data.get("provider_event_receipt_id")
        if not receipt_id:
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=None,
                reason="run_missing_receipt",
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)

        receipt = self._dao.get_receipt_by_id(receipt_id=str(receipt_id))
        if receipt is None:
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=str(receipt_id),
                reason="receipt_not_found",
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)

        return self._authorize_receipt(
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            receipt=receipt,
            event_context_ref=None,
            actor=actor,
            audience=None,
            audit_action=audit_action,
            allow_missing_context=allow_missing_context,
            allow_already_removed=allow_already_removed,
        )

    def _require_receipt(self, receipt_id: str) -> ProviderEventReceipt:
        receipt = self._dao.get_receipt_by_id(receipt_id=receipt_id)
        if receipt is None:
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        return receipt

    def _authorize_receipt(
        self,
        *,
        assistant_id: int,
        task_id: int,
        run_id: int,
        receipt: ProviderEventReceipt,
        event_context_ref: str | None,
        actor: str,
        audience: str | None,
        audit_action: BlobAuditAction,
        allow_missing_context: bool = False,
        allow_already_removed: bool = False,
    ) -> ProviderEventReceipt:
        binding = self._dao.get_binding(binding_id=receipt.binding_id)
        if binding is None:
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="binding_not_found",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        if binding.assistant_id != assistant_id or binding.task_id != task_id:
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="binding_ownership_mismatch",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        if receipt.run_id is None or int(receipt.run_id) != int(run_id):
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="run_receipt_mismatch",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)

        if event_context_ref is not None and (
            not receipt.event_context_ref
            or receipt.event_context_ref != event_context_ref
        ):
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="event_context_ref_mismatch",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)

        if (
            receipt.event_context_unavailable_reason
            == EventContextUnavailableReason.deleted.value
        ):
            if allow_already_removed:
                return receipt
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="context_deleted",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.deleted)

        if (
            receipt.event_context_unavailable_reason
            == EventContextUnavailableReason.expired.value
        ):
            if allow_already_removed:
                return receipt
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="context_expired",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.expired)

        if not allow_missing_context and not receipt.event_context_ref:
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="context_unavailable",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable)

        if (
            receipt.event_context_expires_at is not None
            and datetime.now(timezone.utc) >= receipt.event_context_expires_at
        ):
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="context_expired",
                audience=audience,
                audit_action=audit_action,
            )
            raise EventContextAccessError(EventContextErrorReason.expired)

        return receipt

    def _decrypt_bundle(
        self,
        *,
        receipt: ProviderEventReceipt,
        assistant_id: int,
        task_id: int,
        run_id: int,
        actor: str,
        audience: str | None,
        audit_action: BlobAuditAction,
    ) -> ResolvedEventContext:
        if not receipt.event_context_ref:
            raise EventContextAccessError(EventContextErrorReason.unavailable)

        try:
            source_bytes = self._blob_service.read_authorized(
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                actor=actor,
                audience=audience,
                audit_action=audit_action,
            )
        except (PermissionError, EventBlobAuthenticationError) as exc:
            self._audit_denied(
                actor=actor,
                assistant_id=assistant_id,
                task_id=task_id,
                receipt_id=receipt.receipt_id,
                reason="decrypt_failed",
                audience=audience,
                audit_action=BlobAuditAction.read_denied,
            )
            raise EventContextAccessError(EventContextErrorReason.unavailable) from exc

        return ResolvedEventContext(
            receipt_id=receipt.receipt_id,
            run_id=run_id,
            event_context_ref=receipt.event_context_ref,
            envelope=dict(receipt.stable_envelope_json or {}),
            curated_projection=dict(receipt.curated_projection_json or {}),
            source_body=_parse_source_body(source_bytes),
            expires_at=receipt.event_context_expires_at,
        )

    def _load_owned_run(
        self,
        *,
        project_id: int,
        assistant_id: int,
        task_id: int,
        run_id: int,
    ):
        run = get_task_run_by_run_id(self._session, project_id, run_id=run_id)
        if run is None:
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        run_data = dict(run.data or {})
        if str(run_data.get("task_id")) != str(task_id):
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        run_assistant_id = run_data.get("assistant_id")
        if run_assistant_id is not None and str(run_assistant_id) != str(assistant_id):
            raise EventContextAccessError(EventContextErrorReason.unavailable)
        return run, run_data

    def _audit_denied(
        self,
        *,
        actor: str,
        assistant_id: int,
        task_id: int,
        receipt_id: str | None,
        reason: str,
        audience: str | None = None,
        audit_action: BlobAuditAction = BlobAuditAction.read_denied,
    ) -> None:
        self._dao.record_blob_audit(
            action=audit_action,
            actor=actor,
            audience=audience,
            receipt_id=receipt_id,
            assistant_id=assistant_id,
            task_id=task_id,
            reason=reason,
        )


def _parse_source_body(raw: bytes):
    """Return decrypted body as parsed JSON when possible."""

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")
