"""Physical deletion and orphan cleanup for private provider-event blobs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import (
    ProviderEventBlob,
    ProviderEventBlobDeletion,
)
from orchestra.provider_triggers.private_event_storage import (
    EventBlobService,
    create_event_blob_service,
)
from orchestra.provider_triggers.runtime_types import (
    BlobAuditAction,
    BlobCommitState,
    BlobDeletionState,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_DELETION_LEASE = timedelta(minutes=5)
MAX_DELETION_ATTEMPTS = 5


class ProviderEventBlobCleanupService:
    """Delete queued ciphertext and sweep stale uncommitted blobs."""

    def __init__(
        self,
        session: Session,
        *,
        blob_service: EventBlobService | None = None,
    ) -> None:
        self._session = session
        self._dao = ProviderTriggerDAO(session)
        self._blob_service = blob_service or create_event_blob_service()
        self._lease_owner = f"blob-cleanup-{uuid.uuid4().hex[:8]}"

    def process_deletion_batch(self) -> int:
        """Claim and process one batch of queued blob deletions."""

        batch_size = settings.trigger_event_deletion_batch_size
        now = datetime.now(timezone.utc)
        claimable = (
            select(ProviderEventBlobDeletion)
            .where(
                ProviderEventBlobDeletion.processing_state.in_(
                    [
                        BlobDeletionState.pending.value,
                        BlobDeletionState.retryable.value,
                    ],
                ),
                or_(
                    ProviderEventBlobDeletion.next_retry_at.is_(None),
                    ProviderEventBlobDeletion.next_retry_at <= now,
                ),
                or_(
                    ProviderEventBlobDeletion.lease_expires_at.is_(None),
                    ProviderEventBlobDeletion.lease_expires_at <= now,
                ),
            )
            .order_by(ProviderEventBlobDeletion.id.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        tasks = list(self._session.execute(claimable).scalars())
        processed = 0
        for task in tasks:
            task.processing_state = BlobDeletionState.claimed.value
            task.lease_owner = self._lease_owner
            task.lease_expires_at = now + DEFAULT_DELETION_LEASE
            task.attempt_count += 1
            self._session.flush()
            try:
                self._delete_task(task)
                processed += 1
            except Exception:
                logger.exception(
                    "provider-event blob deletion failed for blob_id=%s",
                    task.blob_id,
                )
                task.processing_state = (
                    BlobDeletionState.failed.value
                    if task.attempt_count >= MAX_DELETION_ATTEMPTS
                    else BlobDeletionState.retryable.value
                )
                task.next_retry_at = now + DEFAULT_DELETION_LEASE
                task.lease_owner = None
                task.lease_expires_at = None
                self._session.flush()
        return processed

    def sweep_orphan_uncommitted(self) -> int:
        """Delete uncommitted blobs older than the configured safety interval."""

        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.trigger_event_orphan_safety_seconds,
        )
        orphans = self._session.execute(
            select(ProviderEventBlob).where(
                ProviderEventBlob.commit_state == BlobCommitState.uncommitted.value,
                ProviderEventBlob.created_at <= cutoff,
            ),
        ).scalars()
        removed = 0
        for blob in orphans:
            try:
                self._blob_service.delete_ciphertext(namespace_key=blob.namespace_key)
            except FileNotFoundError:
                pass
            blob.commit_state = BlobCommitState.deleted.value
            blob.deleted_at = datetime.now(timezone.utc)
            self._dao.record_blob_audit(
                action=BlobAuditAction.delete,
                actor="system",
                blob=blob,
                reason="orphan_sweep",
            )
            removed += 1
        self._session.flush()
        return removed

    def _delete_task(self, task: ProviderEventBlobDeletion) -> None:
        blob = self._session.execute(
            select(ProviderEventBlob).where(
                ProviderEventBlob.blob_id == task.blob_id,
            ),
        ).scalar_one_or_none()
        if blob is None:
            task.processing_state = BlobDeletionState.succeeded.value
            task.terminal_reason = "blob_row_missing"
            task.lease_owner = None
            task.lease_expires_at = None
            self._session.flush()
            return

        try:
            self._blob_service.delete_ciphertext(namespace_key=task.namespace_key)
        except FileNotFoundError:
            pass
        now = datetime.now(timezone.utc)
        blob.commit_state = BlobCommitState.deleted.value
        blob.deleted_at = now
        task.processing_state = BlobDeletionState.succeeded.value
        task.terminal_reason = "deleted"
        task.lease_owner = None
        task.lease_expires_at = None
        self._dao.record_blob_audit(
            action=BlobAuditAction.delete,
            actor="system",
            blob=blob,
            reason="deletion_queue",
        )
        self._session.flush()
