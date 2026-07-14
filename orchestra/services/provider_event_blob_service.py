"""Orchestration for private provider-event blob lifecycle."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    ProviderEventBlob,
    ProviderEventReceipt,
)
from orchestra.provider_triggers.private_event_storage import (
    EncryptedEventObject,
    EventBlobAuthenticationError,
    EventBlobService,
    TriggerKeyWrappingService,
    WrappedKeyMaterial,
    binding_dedup_wrap_purpose,
    create_event_blob_service,
)
from orchestra.provider_triggers.runtime_types import BlobAuditAction


class ProviderEventBlobService:
    """Coordinates encrypted blob storage with durable metadata rows."""

    def __init__(
        self,
        session: Session,
        *,
        blob_service: EventBlobService | None = None,
        wrapping_service: TriggerKeyWrappingService | None = None,
    ) -> None:
        self._session = session
        self._dao = ProviderTriggerDAO(session)
        self._blob_service = blob_service or create_event_blob_service()
        self._wrapping = wrapping_service or self._blob_service.wrapping_service

    def write_uncommitted(
        self,
        *,
        binding_id: str,
        receipt_id: str,
        plaintext: bytes,
        content_type: str = "application/json",
        actor: str = "system",
    ) -> ProviderEventBlob:
        """Encrypt, persist ciphertext, and record an uncommitted blob row."""

        encrypted, ciphertext = self._blob_service.encrypt_object(
            binding_id=binding_id,
            receipt_id=receipt_id,
            plaintext=plaintext,
            content_type=content_type,
        )
        blob = self._dao.create_uncommitted_blob(
            blob_id=f"blob-{uuid.uuid4().hex[:12]}",
            binding_id=binding_id,
            receipt_id=receipt_id,
            encrypted=encrypted,
        )
        try:
            self._blob_service.write_ciphertext(encrypted, ciphertext=ciphertext)
        except Exception:
            self._session.delete(blob)
            self._session.flush()
            raise
        self._dao.record_blob_audit(
            action=BlobAuditAction.write,
            actor=actor,
            blob=blob,
            reason="uncommitted_write",
        )
        return blob

    def attach_event_context(
        self,
        *,
        receipt: ProviderEventReceipt,
        blob: ProviderEventBlob,
        actor: str = "system",
    ) -> ProviderEventReceipt:
        """Commit one uncommitted blob and attach thin refs to the receipt."""

        receipt = self._dao.attach_event_context(
            receipt=receipt,
            blob=blob,
        )
        self._dao.record_blob_audit(
            action=BlobAuditAction.write,
            actor=actor,
            blob=blob,
            receipt=receipt,
            reason="committed_attach",
        )
        return receipt

    def read_authorized(
        self,
        *,
        assistant_id: int,
        task_id: int,
        receipt_id: str,
        actor: str,
        audience: str | None = None,
        audit_action: BlobAuditAction = BlobAuditAction.read,
    ) -> bytes:
        """Decrypt one committed blob after ownership checks."""

        blob, receipt, binding = self._dao.get_event_blob_for_authorized_read(
            assistant_id=assistant_id,
            task_id=task_id,
            receipt_id=receipt_id,
        )
        if blob is None or receipt is None or binding is None:
            self._dao.record_blob_audit(
                action=BlobAuditAction.read_denied,
                actor=actor,
                audience=audience,
                receipt_id=receipt_id,
                assistant_id=assistant_id,
                task_id=task_id,
                reason="not_found_or_unauthorized",
            )
            raise PermissionError("event context is not available")

        encrypted = encrypted_from_blob(blob)
        try:
            plaintext = self._blob_service.read_object(
                encrypted,
                binding_id=blob.binding_id,
                receipt_id=blob.receipt_id,
            )
        except EventBlobAuthenticationError:
            self._dao.record_blob_audit(
                action=BlobAuditAction.read_denied,
                actor=actor,
                audience=audience,
                blob=blob,
                receipt=receipt,
                assistant_id=assistant_id,
                task_id=task_id,
                reason="authentication_failed",
            )
            raise

        self._dao.record_blob_audit(
            action=audit_action,
            actor=actor,
            audience=audience,
            blob=blob,
            receipt=receipt,
            assistant_id=assistant_id,
            task_id=task_id,
        )
        return plaintext

    def mark_event_context_unavailable(
        self,
        *,
        receipt: ProviderEventReceipt,
        actor: str = "system",
        reason: str = "context_unavailable",
    ) -> ProviderEventReceipt:
        """Mark one receipt's event context unavailable and queue deletion."""

        receipt, blob = self._dao.mark_event_context_unavailable(
            receipt=receipt,
            reason=reason,
        )
        if blob is not None:
            self._dao.enqueue_blob_deletion(blob=blob)
            self._dao.record_blob_audit(
                action=BlobAuditAction.mark_unavailable,
                actor=actor,
                blob=blob,
                receipt=receipt,
                reason=reason,
            )
        return receipt

    def ensure_binding_dedup_key(
        self,
        *,
        binding: EventTriggerBinding,
    ) -> bytes:
        """Return the immutable binding HMAC key, creating it when absent."""

        locked = self._dao.get_binding(binding_id=binding.binding_id, for_update=True)
        if locked is None:
            raise ValueError("binding_not_found")
        binding = locked

        if binding.dedup_key_secret_ref and binding.dedup_key_wrapping_version:
            wrapped = WrappedKeyMaterial(
                wrapping_key_version=binding.dedup_key_wrapping_version,
                wrapped_key=base64.b64decode(binding.dedup_key_secret_ref),
                algorithm=self._infer_wrap_algorithm(
                    binding.dedup_key_wrapping_version,
                ),
            )
            return self._wrapping.unwrap_key(
                wrapped,
                purpose=binding_dedup_wrap_purpose(binding_id=binding.binding_id),
            )

        key_material = self._wrapping.generate_binding_hmac_key()
        wrapped = self._wrapping.wrap_key(
            purpose=binding_dedup_wrap_purpose(binding_id=binding.binding_id),
            key_material=key_material,
        )
        binding.dedup_key_secret_ref = base64.b64encode(wrapped.wrapped_key).decode(
            "ascii",
        )
        binding.dedup_key_wrapping_version = wrapped.wrapping_key_version
        self._session.flush()
        return key_material

    def rewrap_blob(self, *, blob: ProviderEventBlob) -> ProviderEventBlob:
        """Rewrap one blob's data key under the current wrapping version."""

        encrypted = encrypted_from_blob(blob)
        rewrapped = self._blob_service.rewrap_object_metadata(
            encrypted,
            binding_id=blob.binding_id,
            receipt_id=blob.receipt_id,
        )
        blob.wrap_algorithm = rewrapped.wrap_algorithm
        blob.wrapping_key_version = rewrapped.wrapping_key_version
        blob.wrapped_data_key = rewrapped.wrapped_data_key
        blob.updated_at = datetime.now(timezone.utc)
        self._session.flush()
        self._dao.record_blob_audit(
            action=BlobAuditAction.rewrap,
            actor="system",
            blob=blob,
        )
        return blob

    @staticmethod
    def _infer_wrap_algorithm(wrapping_key_version: str) -> str:
        if wrapping_key_version.startswith("kms:"):
            from orchestra.provider_triggers.private_event_storage import (
                KMS_WRAP_ALGORITHM,
            )

            return KMS_WRAP_ALGORITHM
        from orchestra.provider_triggers.private_event_storage import (
            SELF_HOST_WRAP_ALGORITHM,
        )

        return SELF_HOST_WRAP_ALGORITHM


def encrypted_from_blob(blob: ProviderEventBlob) -> EncryptedEventObject:
    """Build storage metadata from one persisted blob row."""

    return EncryptedEventObject(
        namespace_key=blob.namespace_key,
        algorithm=blob.algorithm,
        wrap_algorithm=blob.wrap_algorithm,
        wrapping_key_version=blob.wrapping_key_version,
        wrapped_data_key=bytes(blob.wrapped_data_key),
        integrity_hash=blob.integrity_hash,
        size_bytes=blob.size_bytes,
        content_type=blob.content_type,
    )


def receipt_event_context_ref(blob: ProviderEventBlob) -> str:
    """Return the opaque reference stored on receipt rows."""

    return blob.blob_id
