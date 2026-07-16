"""Durable operational state for provider-event task triggers."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from orchestra.db.base import Base

JSON_EMPTY_ARRAY = sa.text("'[]'::jsonb")
JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


class EventTriggerBinding(Base):
    """Derived runtime binding for one provider-event task lifetime."""

    __tablename__ = "event_trigger_bindings"

    id = Column(Integer, primary_key=True)
    binding_id = Column(String(64), nullable=False, unique=True, index=True)
    project_id = Column(Integer, nullable=False, index=True)
    tasks_context_id = Column(Integer, nullable=False, index=True)
    source_task_log_id = Column(Integer, nullable=False)
    task_id = Column(Integer, nullable=False, index=True)
    assistant_id = Column(Integer, nullable=False, index=True)

    task_revision = Column(Integer, nullable=False, server_default="1")
    desired_trigger_state = Column(String(32), nullable=False, server_default="draft")
    desired_activation_revision = Column(String(64), nullable=False)
    acceptance_epoch = Column(Integer, nullable=False, server_default="1")

    connection_id = Column(String, nullable=False)
    backend_id = Column(String, nullable=False, index=True)
    canonical_app_slug = Column(String, nullable=False)
    provider_trigger_slug = Column(String, nullable=False)
    trigger_config_json = Column(
        JSONB, nullable=False, server_default=JSON_EMPTY_OBJECT
    )
    execution_mode = Column(String(16), nullable=False, server_default="live")
    entrypoint = Column(Integer, nullable=True)

    owner_scope = Column(String(32), nullable=False, server_default="assistant")
    provider_account_subject_hmac = Column(String(128), nullable=True)
    provider_account_display_label = Column(String, nullable=True)

    observed_activation_revision = Column(String(64), nullable=True)
    active_generation_id = Column(String(64), nullable=True, index=True)
    runtime_health = Column(
        String(32),
        nullable=False,
        server_default="absent",
        index=True,
    )
    local_acceptance_open = Column(
        Boolean,
        nullable=False,
        server_default=sa.text("false"),
    )

    dedup_key_secret_ref = Column(String, nullable=True)
    dedup_key_wrapping_version = Column(String, nullable=True)

    reconcile_attempt_count = Column(Integer, nullable=False, server_default="0")
    reconcile_next_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)
    reconcile_lease_owner = Column(String, nullable=True)
    reconcile_lease_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    reconcile_processing_started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_stable_error_code = Column(String, nullable=True)

    coverage_started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    coverage_ended_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_accepted_event_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_health_check_at = Column(TIMESTAMP(timezone=True), nullable=True)
    consecutive_health_failures = Column(Integer, nullable=False, server_default="0")

    tombstoned_at = Column(TIMESTAMP(timezone=True), nullable=True)
    teardown_completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "source_task_log_id",
            name="uq_event_trigger_binding_task_lifetime",
        ),
        Index(
            "ix_event_trigger_bindings_reconcile_claim",
            "reconcile_next_retry_at",
            "reconcile_lease_expires_at",
        ),
    )


class EventTriggerSubscriptionGeneration(Base):
    """One versioned provider subscription for a binding."""

    __tablename__ = "event_trigger_subscription_generations"

    id = Column(Integer, primary_key=True)
    generation_id = Column(String(64), nullable=False, unique=True, index=True)
    binding_id = Column(
        String(64),
        ForeignKey("event_trigger_bindings.binding_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    desired_activation_revision = Column(String(64), nullable=False)
    acceptance_epoch = Column(Integer, nullable=False)

    provider_create_idempotency_key = Column(String(128), nullable=False)
    provider_delete_idempotency_key = Column(String(128), nullable=True)
    external_trigger_id = Column(String, nullable=True, index=True)
    ingress_key = Column(String(64), nullable=False, unique=True, index=True)

    signing_secret_ref = Column(String, nullable=True)
    signing_secret_version = Column(String, nullable=True)
    previous_signing_secret_ref = Column(String, nullable=True)
    previous_signing_secret_version = Column(String, nullable=True)
    signing_overlap_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)

    lifecycle_state = Column(
        String(32),
        nullable=False,
        server_default="provisioning",
        index=True,
    )
    create_operation_state = Column(String(32), nullable=True)
    delete_operation_state = Column(String(32), nullable=True)

    attempt_count = Column(Integer, nullable=False, server_default="0")
    next_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)
    lease_owner = Column(String, nullable=True)
    lease_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_stable_error_code = Column(String, nullable=True)

    provider_confirmed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    activated_at = Column(TIMESTAMP(timezone=True), nullable=True)
    deactivated_at = Column(TIMESTAMP(timezone=True), nullable=True)
    teardown_completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ux_event_trigger_generation_active_binding",
            "binding_id",
            unique=True,
            postgresql_where=text("lifecycle_state = 'active'"),
        ),
        Index(
            "ix_event_trigger_generation_claim",
            "next_retry_at",
            "lease_expires_at",
        ),
    )


class ProviderEventReceipt(Base):
    """One durable provider delivery receipt."""

    __tablename__ = "provider_event_receipts"

    id = Column(Integer, primary_key=True)
    receipt_id = Column(String(64), nullable=False, unique=True, index=True)
    binding_id = Column(
        String(64),
        ForeignKey("event_trigger_bindings.binding_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_id = Column(
        String(64),
        ForeignKey(
            "event_trigger_subscription_generations.generation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    provider_event_identity_hmac = Column(String(128), nullable=False)

    accepted_activation_revision = Column(String(64), nullable=False)
    acceptance_epoch = Column(Integer, nullable=False)
    schema_version = Column(String, nullable=False)

    processing_state = Column(
        String(32),
        nullable=False,
        server_default="accepted",
        index=True,
    )
    attempt_count = Column(Integer, nullable=False, server_default="0")
    next_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)

    acceptance_authorization_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_OBJECT,
    )
    classification_reason = Column(String, nullable=True)

    stable_envelope_json = Column(JSONB, nullable=True)
    curated_projection_json = Column(JSONB, nullable=True)
    event_context_ref = Column(String, nullable=True)
    event_context_integrity_hash = Column(String, nullable=True)
    event_context_size_bytes = Column(Integer, nullable=True)
    event_context_content_type = Column(String, nullable=True)
    event_context_key_version = Column(String, nullable=True)
    event_context_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    event_context_unavailable_reason = Column(String, nullable=True)

    run_key = Column(String(128), nullable=True, index=True)
    run_id = Column(Integer, nullable=True)
    dispatch_mode = Column(String(16), nullable=True)
    dispatch_operation_id = Column(String(64), nullable=True, index=True)

    received_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    first_attempt_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_attempt_at = Column(TIMESTAMP(timezone=True), nullable=True)
    terminal_at = Column(TIMESTAMP(timezone=True), nullable=True)
    terminal_reason = Column(String, nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "provider_event_identity_hmac",
            name="uq_provider_event_receipt_binding_identity",
        ),
        Index(
            "ix_provider_event_receipt_claim",
            "next_retry_at",
            "processing_state",
        ),
    )


class ProviderEventDispatch(Base):
    """One durable dispatch operation for an accepted provider-event run."""

    __tablename__ = "provider_event_dispatches"

    id = Column(Integer, primary_key=True)
    operation_id = Column(String(64), nullable=False, unique=True, index=True)
    receipt_id = Column(
        String(64),
        ForeignKey("provider_event_receipts.receipt_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    binding_id = Column(
        String(64),
        ForeignKey("event_trigger_bindings.binding_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assistant_id = Column(Integer, nullable=False, index=True)
    task_id = Column(Integer, nullable=False, index=True)
    run_id = Column(Integer, nullable=False)
    run_key = Column(String(128), nullable=False, unique=True)

    dispatch_mode = Column(String(16), nullable=False)
    accepted_activation_revision = Column(String(64), nullable=False)
    event_context_ref = Column(String, nullable=True)
    audience = Column(String, nullable=False)

    processing_state = Column(
        String(32),
        nullable=False,
        server_default="pending",
        index=True,
    )
    lease_owner = Column(String, nullable=True)
    lease_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    next_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)

    downstream_adoption_status = Column(String, nullable=True)
    downstream_adoption_ref = Column(String, nullable=True)
    downstream_status_at = Column(TIMESTAMP(timezone=True), nullable=True)
    terminal_error_code = Column(String, nullable=True)

    delivered_at = Column(TIMESTAMP(timezone=True), nullable=True)
    started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    terminal_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_provider_event_dispatch_claim",
            "next_retry_at",
            "lease_expires_at",
            "processing_state",
        ),
    )


class ProviderEventBlob(Base):
    """Private encrypted provider-event payload metadata."""

    __tablename__ = "provider_event_blobs"

    id = Column(Integer, primary_key=True)
    blob_id = Column(String(64), nullable=False, unique=True, index=True)
    namespace_key = Column(String(256), nullable=False, unique=True, index=True)
    binding_id = Column(
        String(64),
        ForeignKey("event_trigger_bindings.binding_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    receipt_id = Column(String(64), nullable=False, index=True)

    algorithm = Column(String(32), nullable=False)
    wrap_algorithm = Column(String(32), nullable=False)
    wrapping_key_version = Column(String(128), nullable=False)
    wrapped_data_key = Column(sa.LargeBinary, nullable=False)
    integrity_hash = Column(String(128), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    content_type = Column(String, nullable=False)

    commit_state = Column(
        String(32),
        nullable=False,
        server_default="uncommitted",
        index=True,
    )
    committed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    unavailable_at = Column(TIMESTAMP(timezone=True), nullable=True)
    deleted_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_provider_event_blob_orphan_sweep",
            "commit_state",
            "created_at",
        ),
    )


class ProviderEventBlobAudit(Base):
    """Append-only audit trail for private provider-event blob access."""

    __tablename__ = "provider_event_blob_audit"

    id = Column(Integer, primary_key=True)
    action = Column(String(32), nullable=False, index=True)
    actor = Column(String, nullable=False)
    audience = Column(String, nullable=True)
    blob_id = Column(String(64), nullable=True, index=True)
    binding_id = Column(String(64), nullable=True, index=True)
    receipt_id = Column(String(64), nullable=True, index=True)
    assistant_id = Column(Integer, nullable=True, index=True)
    task_id = Column(Integer, nullable=True, index=True)
    reason = Column(String, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), index=True)


class ProviderEventBlobDeletion(Base):
    """Queue for physical deletion of private provider-event ciphertext."""

    __tablename__ = "provider_event_blob_deletions"

    id = Column(Integer, primary_key=True)
    blob_id = Column(
        String(64),
        ForeignKey("provider_event_blobs.blob_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    namespace_key = Column(String(256), nullable=False)

    processing_state = Column(
        String(32),
        nullable=False,
        server_default="pending",
        index=True,
    )
    lease_owner = Column(String, nullable=True)
    lease_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    next_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)
    terminal_reason = Column(String, nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_provider_event_blob_deletion_claim",
            "next_retry_at",
            "lease_expires_at",
            "processing_state",
        ),
    )


class ProviderTriggerWorkerHeartbeat(Base):
    """Last-seen heartbeat for the provider-trigger worker."""

    __tablename__ = "provider_trigger_worker_heartbeats"

    id = Column(Integer, primary_key=True)
    worker_key = Column(String(64), nullable=False, unique=True, index=True)
    lease_owner = Column(String, nullable=True)
    last_reconcile_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_health_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_heartbeat_at = Column(TIMESTAMP(timezone=True), nullable=False)
    metadata_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_OBJECT)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
