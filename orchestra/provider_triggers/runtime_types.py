"""Runtime vocabulary for provider-event trigger operational state."""

from __future__ import annotations

from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


class BindingRuntimeHealth(StrEnum):
    """Derived health for one provider-event binding."""

    absent = "absent"
    provisioning = "provisioning"
    healthy = "healthy"
    recovering = "recovering"
    needs_attention = "needs_attention"
    removing = "removing"


class DesiredTriggerState(StrEnum):
    """Authored provider-trigger automation state mirrored on the binding."""

    draft = "draft"
    enabled = "enabled"
    paused = "paused"


class GenerationLifecycle(StrEnum):
    """Lifecycle for one provider subscription generation."""

    provisioning = "provisioning"
    active = "active"
    draining = "draining"
    removing = "removing"
    removed = "removed"
    failed = "failed"


class ReceiptProcessingState(StrEnum):
    """Processing state for one durable provider-event receipt."""

    ignored = "ignored"
    accepted = "accepted"
    dispatch_pending = "dispatch_pending"
    dispatched = "dispatched"
    started = "started"
    succeeded = "succeeded"
    retryable = "retryable"
    failed = "failed"


class DispatchProcessingState(StrEnum):
    """Processing state for one provider-event dispatch operation."""

    pending = "pending"
    claimed = "claimed"
    delivered = "delivered"
    started = "started"
    succeeded = "succeeded"
    retryable = "retryable"
    failed = "failed"


class BlobCommitState(StrEnum):
    """Commit lifecycle for one private provider-event blob."""

    uncommitted = "uncommitted"
    committed = "committed"
    unavailable = "unavailable"
    deleted = "deleted"


class BlobAuditAction(StrEnum):
    """Append-only audit actions for private provider-event blobs."""

    write = "write"
    read = "read"
    read_denied = "read_denied"
    rewrap = "rewrap"
    export = "export"
    mark_unavailable = "mark_unavailable"
    delete = "delete"


class BlobDeletionState(StrEnum):
    """Processing state for one queued blob deletion."""

    pending = "pending"
    claimed = "claimed"
    succeeded = "succeeded"
    retryable = "retryable"
    failed = "failed"
