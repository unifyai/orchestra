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


class GenerationOperationState(StrEnum):
    """Processing state for one provider create/delete operation."""

    pending = "pending"
    claimed = "claimed"
    succeeded = "succeeded"
    retryable = "retryable"
    failed = "failed"


class ReconcileErrorCode(StrEnum):
    """Stable internal error codes for provider-trigger reconciliation."""

    event_storage_unconfigured = "event_storage_unconfigured"
    callback_url_unconfigured = "callback_url_unconfigured"
    signing_secret_unconfigured = "signing_secret_unconfigured"
    worker_unhealthy = "worker_unhealthy"
    provider_connection_missing = "provider_connection_missing"
    connection_not_owned = "connection_not_owned"
    account_subject_mismatch = "account_subject_mismatch"
    resource_inaccessible = "resource_inaccessible"
    unsupported_event = "unsupported_event"
    provider_permission_denied = "provider_permission_denied"
    provider_provision_failed = "provider_provision_failed"
    provider_delete_failed = "provider_delete_failed"
    provider_health_check_failed = "provider_health_check_failed"
    provider_connection_not_active = "provider_connection_not_active"
    trigger_delivery_only = "trigger_delivery_only"
    trigger_not_live_ready = "trigger_not_live_ready"
    required_config_missing = "required_config_missing"


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


class ReceiptClassificationReason(StrEnum):
    """Stable classification for one durable provider-event receipt."""

    matched = "matched"
    unmatched = "unmatched"
    inactive = "inactive"
    stale = "stale"
    unauthorized = "unauthorized"
    unsupported = "unsupported"


class DispatchProcessingState(StrEnum):
    """Processing state for one provider-event dispatch operation."""

    pending = "pending"
    claimed = "claimed"
    delivered = "delivered"
    started = "started"
    succeeded = "succeeded"
    retryable = "retryable"
    failed = "failed"


class DispatchErrorCode(StrEnum):
    """Stable internal error codes for provider-event dispatch delivery."""

    dispatch_rail_unconfigured = "dispatch_rail_unconfigured"
    dispatch_request_expired = "dispatch_request_expired"
    dispatch_validation_failed = "dispatch_validation_failed"
    dispatch_inbox_mismatch = "dispatch_inbox_mismatch"
    dispatch_transport_failed = "dispatch_transport_failed"
    dispatch_downstream_rejected = "dispatch_downstream_rejected"
    dispatch_run_terminal_failed = "dispatch_run_terminal_failed"
    dispatch_max_attempts_exceeded = "dispatch_max_attempts_exceeded"


class DownstreamAdoptionStatus(StrEnum):
    """Public downstream adoption vocabulary mirrored on dispatch rows."""

    adopted = "adopted"
    started = "started"
    terminal = "terminal"
    published = "published"


class BlobCommitState(StrEnum):
    """Commit lifecycle for one private provider-event blob."""

    uncommitted = "uncommitted"
    committed = "committed"
    unavailable = "unavailable"
    deleted = "deleted"


class EventContextUnavailableReason(StrEnum):
    """Why one receipt's event context is no longer readable."""

    deleted = "deleted"
    expired = "expired"


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
