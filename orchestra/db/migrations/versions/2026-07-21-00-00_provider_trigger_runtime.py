"""Provider-event trigger runtime persistence tables.

Adds durable bindings, subscription generations, receipts, and dispatch
operations for third-party task triggers. Constraint and index names match
the ORM models so fresh test databases and migrated production agree.

Revision ID: provider_trigger_runtime
Revises: integration_conn_provider_uid
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "provider_trigger_runtime"
down_revision = "integration_conn_provider_uid"
branch_labels = None
depends_on = None

JSON_EMPTY_ARRAY = sa.text("'[]'::jsonb")
JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "event_trigger_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("tasks_context_id", sa.Integer(), nullable=False),
        sa.Column("source_task_log_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "desired_trigger_state",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("desired_activation_revision", sa.String(length=64), nullable=False),
        sa.Column("acceptance_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("canonical_app_slug", sa.String(), nullable=False),
        sa.Column("event_slug", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column(
            "filters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "execution_mode",
            sa.String(length=16),
            nullable=False,
            server_default="live",
        ),
        sa.Column("entrypoint", sa.Integer(), nullable=True),
        sa.Column(
            "owner_scope",
            sa.String(length=32),
            nullable=False,
            server_default="assistant",
        ),
        sa.Column(
            "provider_account_subject_hmac",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column("provider_account_display_label", sa.String(), nullable=True),
        sa.Column("observed_activation_revision", sa.String(length=64), nullable=True),
        sa.Column("active_generation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "runtime_health",
            sa.String(length=32),
            nullable=False,
            server_default="absent",
        ),
        sa.Column(
            "local_acceptance_open",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("dedup_key_secret_ref", sa.String(), nullable=True),
        sa.Column("dedup_key_wrapping_version", sa.String(), nullable=True),
        sa.Column(
            "reconcile_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reconcile_next_retry_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("reconcile_lease_owner", sa.String(), nullable=True),
        sa.Column(
            "reconcile_lease_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "reconcile_processing_started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("last_stable_error_code", sa.String(), nullable=True),
        sa.Column("coverage_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("coverage_ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_accepted_event_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_health_check_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tombstoned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("teardown_completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "project_id",
            "source_task_log_id",
            name="uq_event_trigger_binding_task_lifetime",
        ),
        sa.UniqueConstraint("binding_id", name="uq_event_trigger_bindings_binding_id"),
    )
    op.create_index(
        "ix_event_trigger_bindings_binding_id",
        "event_trigger_bindings",
        ["binding_id"],
        unique=True,
    )
    op.create_index(
        "ix_event_trigger_bindings_project_id",
        "event_trigger_bindings",
        ["project_id"],
    )
    op.create_index(
        "ix_event_trigger_bindings_tasks_context_id",
        "event_trigger_bindings",
        ["tasks_context_id"],
    )
    op.create_index(
        "ix_event_trigger_bindings_task_id",
        "event_trigger_bindings",
        ["task_id"],
    )
    op.create_index(
        "ix_event_trigger_bindings_assistant_id",
        "event_trigger_bindings",
        ["assistant_id"],
    )
    op.create_index(
        "ix_event_trigger_bindings_backend_id",
        "event_trigger_bindings",
        ["backend_id"],
    )
    op.create_index(
        "ix_event_trigger_bindings_runtime_health",
        "event_trigger_bindings",
        ["runtime_health"],
    )
    op.create_index(
        "ix_event_trigger_bindings_active_generation_id",
        "event_trigger_bindings",
        ["active_generation_id"],
    )
    op.create_index(
        "ix_event_trigger_bindings_reconcile_claim",
        "event_trigger_bindings",
        ["reconcile_next_retry_at", "reconcile_lease_expires_at"],
    )

    op.create_table(
        "event_trigger_subscription_generations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation_id", sa.String(length=64), nullable=False),
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("desired_activation_revision", sa.String(length=64), nullable=False),
        sa.Column("acceptance_epoch", sa.Integer(), nullable=False),
        sa.Column(
            "provider_create_idempotency_key",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "provider_delete_idempotency_key",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column("external_trigger_id", sa.String(), nullable=True),
        sa.Column("ingress_key", sa.String(length=64), nullable=False),
        sa.Column("signing_secret_ref", sa.String(), nullable=True),
        sa.Column("signing_secret_version", sa.String(), nullable=True),
        sa.Column("previous_signing_secret_ref", sa.String(), nullable=True),
        sa.Column("previous_signing_secret_version", sa.String(), nullable=True),
        sa.Column(
            "signing_overlap_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            nullable=False,
            server_default="provisioning",
        ),
        sa.Column("create_operation_state", sa.String(length=32), nullable=True),
        sa.Column("delete_operation_state", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_stable_error_code", sa.String(), nullable=True),
        sa.Column("provider_confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deactivated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("teardown_completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["event_trigger_bindings.binding_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "generation_id",
            name="uq_event_trigger_subscription_generations_generation_id",
        ),
        sa.UniqueConstraint(
            "ingress_key",
            name="uq_event_trigger_subscription_generations_ingress_key",
        ),
    )
    op.create_index(
        "ix_event_trigger_subscription_generations_generation_id",
        "event_trigger_subscription_generations",
        ["generation_id"],
        unique=True,
    )
    op.create_index(
        "ix_event_trigger_subscription_generations_binding_id",
        "event_trigger_subscription_generations",
        ["binding_id"],
    )
    op.create_index(
        "ix_event_trigger_subscription_generations_external_trigger_id",
        "event_trigger_subscription_generations",
        ["external_trigger_id"],
    )
    op.create_index(
        "ix_event_trigger_subscription_generations_ingress_key",
        "event_trigger_subscription_generations",
        ["ingress_key"],
        unique=True,
    )
    op.create_index(
        "ix_event_trigger_subscription_generations_lifecycle_state",
        "event_trigger_subscription_generations",
        ["lifecycle_state"],
    )
    op.create_index(
        "ux_event_trigger_generation_active_binding",
        "event_trigger_subscription_generations",
        ["binding_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'active'"),
    )
    op.create_index(
        "ix_event_trigger_generation_claim",
        "event_trigger_subscription_generations",
        ["next_retry_at", "lease_expires_at"],
    )

    op.create_table(
        "provider_event_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("generation_id", sa.String(length=64), nullable=False),
        sa.Column(
            "provider_event_identity_hmac",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("accepted_activation_revision", sa.String(length=64), nullable=False),
        sa.Column("acceptance_epoch", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column(
            "processing_state",
            sa.String(length=32),
            nullable=False,
            server_default="accepted",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "acceptance_authorization_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column("classification_reason", sa.String(), nullable=True),
        sa.Column(
            "stable_envelope_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "curated_projection_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("event_context_ref", sa.String(), nullable=True),
        sa.Column("event_context_integrity_hash", sa.String(), nullable=True),
        sa.Column("event_context_size_bytes", sa.Integer(), nullable=True),
        sa.Column("event_context_content_type", sa.String(), nullable=True),
        sa.Column("event_context_key_version", sa.String(), nullable=True),
        sa.Column("run_key", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("dispatch_mode", sa.String(length=16), nullable=True),
        sa.Column("dispatch_operation_id", sa.String(length=64), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("first_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("terminal_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["event_trigger_bindings.binding_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["event_trigger_subscription_generations.generation_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "receipt_id",
            name="uq_provider_event_receipts_receipt_id",
        ),
        sa.UniqueConstraint(
            "binding_id",
            "provider_event_identity_hmac",
            name="uq_provider_event_receipt_binding_identity",
        ),
    )
    op.create_index(
        "ix_provider_event_receipts_receipt_id",
        "provider_event_receipts",
        ["receipt_id"],
        unique=True,
    )
    op.create_index(
        "ix_provider_event_receipts_binding_id",
        "provider_event_receipts",
        ["binding_id"],
    )
    op.create_index(
        "ix_provider_event_receipts_generation_id",
        "provider_event_receipts",
        ["generation_id"],
    )
    op.create_index(
        "ix_provider_event_receipts_processing_state",
        "provider_event_receipts",
        ["processing_state"],
    )
    op.create_index(
        "ix_provider_event_receipts_run_key",
        "provider_event_receipts",
        ["run_key"],
    )
    op.create_index(
        "ix_provider_event_receipts_dispatch_operation_id",
        "provider_event_receipts",
        ["dispatch_operation_id"],
    )
    op.create_index(
        "ix_provider_event_receipt_claim",
        "provider_event_receipts",
        ["next_retry_at", "processing_state"],
    )

    op.create_table(
        "provider_event_dispatches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(length=128), nullable=False),
        sa.Column("dispatch_mode", sa.String(length=16), nullable=False),
        sa.Column("accepted_activation_revision", sa.String(length=64), nullable=False),
        sa.Column("event_context_ref", sa.String(), nullable=True),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column(
            "processing_state",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("downstream_adoption_status", sa.String(), nullable=True),
        sa.Column("downstream_adoption_ref", sa.String(), nullable=True),
        sa.Column("downstream_status_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("terminal_error_code", sa.String(), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["event_trigger_bindings.binding_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["provider_event_receipts.receipt_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_provider_event_dispatches_operation_id",
        ),
        sa.UniqueConstraint(
            "receipt_id",
            name="uq_provider_event_dispatches_receipt_id",
        ),
        sa.UniqueConstraint(
            "run_key",
            name="uq_provider_event_dispatches_run_key",
        ),
    )
    op.create_index(
        "ix_provider_event_dispatches_operation_id",
        "provider_event_dispatches",
        ["operation_id"],
        unique=True,
    )
    op.create_index(
        "ix_provider_event_dispatches_binding_id",
        "provider_event_dispatches",
        ["binding_id"],
    )
    op.create_index(
        "ix_provider_event_dispatches_assistant_id",
        "provider_event_dispatches",
        ["assistant_id"],
    )
    op.create_index(
        "ix_provider_event_dispatches_task_id",
        "provider_event_dispatches",
        ["task_id"],
    )
    op.create_index(
        "ix_provider_event_dispatches_processing_state",
        "provider_event_dispatches",
        ["processing_state"],
    )
    op.create_index(
        "ix_provider_event_dispatch_claim",
        "provider_event_dispatches",
        ["next_retry_at", "lease_expires_at", "processing_state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_event_dispatch_claim",
        table_name="provider_event_dispatches",
    )
    op.drop_index(
        "ix_provider_event_dispatches_processing_state",
        table_name="provider_event_dispatches",
    )
    op.drop_index(
        "ix_provider_event_dispatches_task_id",
        table_name="provider_event_dispatches",
    )
    op.drop_index(
        "ix_provider_event_dispatches_assistant_id",
        table_name="provider_event_dispatches",
    )
    op.drop_index(
        "ix_provider_event_dispatches_binding_id",
        table_name="provider_event_dispatches",
    )
    op.drop_index(
        "ix_provider_event_dispatches_operation_id",
        table_name="provider_event_dispatches",
    )
    op.drop_table("provider_event_dispatches")

    op.drop_index(
        "ix_provider_event_receipt_claim",
        table_name="provider_event_receipts",
    )
    op.drop_index(
        "ix_provider_event_receipts_dispatch_operation_id",
        table_name="provider_event_receipts",
    )
    op.drop_index(
        "ix_provider_event_receipts_run_key",
        table_name="provider_event_receipts",
    )
    op.drop_index(
        "ix_provider_event_receipts_processing_state",
        table_name="provider_event_receipts",
    )
    op.drop_index(
        "ix_provider_event_receipts_generation_id",
        table_name="provider_event_receipts",
    )
    op.drop_index(
        "ix_provider_event_receipts_binding_id",
        table_name="provider_event_receipts",
    )
    op.drop_index(
        "ix_provider_event_receipts_receipt_id",
        table_name="provider_event_receipts",
    )
    op.drop_table("provider_event_receipts")

    op.drop_index(
        "ix_event_trigger_generation_claim",
        table_name="event_trigger_subscription_generations",
    )
    op.drop_index(
        "ux_event_trigger_generation_active_binding",
        table_name="event_trigger_subscription_generations",
    )
    op.drop_index(
        "ix_event_trigger_subscription_generations_lifecycle_state",
        table_name="event_trigger_subscription_generations",
    )
    op.drop_index(
        "ix_event_trigger_subscription_generations_ingress_key",
        table_name="event_trigger_subscription_generations",
    )
    op.drop_index(
        "ix_event_trigger_subscription_generations_external_trigger_id",
        table_name="event_trigger_subscription_generations",
    )
    op.drop_index(
        "ix_event_trigger_subscription_generations_binding_id",
        table_name="event_trigger_subscription_generations",
    )
    op.drop_index(
        "ix_event_trigger_subscription_generations_generation_id",
        table_name="event_trigger_subscription_generations",
    )
    op.drop_table("event_trigger_subscription_generations")

    op.drop_index(
        "ix_event_trigger_bindings_reconcile_claim",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_active_generation_id",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_runtime_health",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_backend_id",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_assistant_id",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_task_id",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_tasks_context_id",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_project_id",
        table_name="event_trigger_bindings",
    )
    op.drop_index(
        "ix_event_trigger_bindings_binding_id",
        table_name="event_trigger_bindings",
    )
    op.drop_table("event_trigger_bindings")
