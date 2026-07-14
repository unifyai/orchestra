"""Private provider-event blob storage tables.

Adds encrypted blob metadata, audit records, and deletion queue rows for
matched provider-event payloads.

Revision ID: provider_event_blob_storage
Revises: provider_trigger_runtime
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "provider_event_blob_storage"
down_revision = "provider_trigger_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_event_blobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("blob_id", sa.String(length=64), nullable=False),
        sa.Column("namespace_key", sa.String(length=256), nullable=False),
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("wrap_algorithm", sa.String(length=32), nullable=False),
        sa.Column("wrapping_key_version", sa.String(length=128), nullable=False),
        sa.Column("wrapped_data_key", sa.LargeBinary(), nullable=False),
        sa.Column("integrity_hash", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column(
            "commit_state",
            sa.String(length=32),
            nullable=False,
            server_default="uncommitted",
        ),
        sa.Column("committed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("unavailable_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["event_trigger_bindings.binding_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("blob_id", name="uq_provider_event_blob_id"),
        sa.UniqueConstraint("namespace_key", name="uq_provider_event_blob_namespace"),
    )
    op.create_index(
        "ix_provider_event_blobs_blob_id",
        "provider_event_blobs",
        ["blob_id"],
        unique=True,
    )
    op.create_index(
        "ix_provider_event_blobs_namespace_key",
        "provider_event_blobs",
        ["namespace_key"],
        unique=True,
    )
    op.create_index(
        "ix_provider_event_blobs_binding_id",
        "provider_event_blobs",
        ["binding_id"],
    )
    op.create_index(
        "ix_provider_event_blobs_receipt_id",
        "provider_event_blobs",
        ["receipt_id"],
    )
    op.create_index(
        "ix_provider_event_blobs_commit_state",
        "provider_event_blobs",
        ["commit_state"],
    )
    op.create_index(
        "ix_provider_event_blob_orphan_sweep",
        "provider_event_blobs",
        ["commit_state", "created_at"],
    )

    op.create_table(
        "provider_event_blob_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=True),
        sa.Column("blob_id", sa.String(length=64), nullable=True),
        sa.Column("binding_id", sa.String(length=64), nullable=True),
        sa.Column("receipt_id", sa.String(length=64), nullable=True),
        sa.Column("assistant_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_provider_event_blob_audit_action",
        "provider_event_blob_audit",
        ["action"],
    )
    op.create_index(
        "ix_provider_event_blob_audit_blob_id",
        "provider_event_blob_audit",
        ["blob_id"],
    )
    op.create_index(
        "ix_provider_event_blob_audit_binding_id",
        "provider_event_blob_audit",
        ["binding_id"],
    )
    op.create_index(
        "ix_provider_event_blob_audit_receipt_id",
        "provider_event_blob_audit",
        ["receipt_id"],
    )
    op.create_index(
        "ix_provider_event_blob_audit_assistant_id",
        "provider_event_blob_audit",
        ["assistant_id"],
    )
    op.create_index(
        "ix_provider_event_blob_audit_task_id",
        "provider_event_blob_audit",
        ["task_id"],
    )
    op.create_index(
        "ix_provider_event_blob_audit_created_at",
        "provider_event_blob_audit",
        ["created_at"],
    )

    op.create_table(
        "provider_event_blob_deletions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("blob_id", sa.String(length=64), nullable=False),
        sa.Column("namespace_key", sa.String(length=256), nullable=False),
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
        sa.Column("terminal_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["blob_id"],
            ["provider_event_blobs.blob_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("blob_id", name="uq_provider_event_blob_deletion_blob"),
    )
    op.create_index(
        "ix_provider_event_blob_deletions_processing_state",
        "provider_event_blob_deletions",
        ["processing_state"],
    )
    op.create_index(
        "ix_provider_event_blob_deletion_claim",
        "provider_event_blob_deletions",
        ["next_retry_at", "lease_expires_at", "processing_state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_event_blob_deletion_claim",
        table_name="provider_event_blob_deletions",
    )
    op.drop_index(
        "ix_provider_event_blob_deletions_processing_state",
        table_name="provider_event_blob_deletions",
    )
    op.drop_table("provider_event_blob_deletions")
    op.drop_index(
        "ix_provider_event_blob_audit_created_at",
        table_name="provider_event_blob_audit",
    )
    op.drop_index(
        "ix_provider_event_blob_audit_task_id",
        table_name="provider_event_blob_audit",
    )
    op.drop_index(
        "ix_provider_event_blob_audit_assistant_id",
        table_name="provider_event_blob_audit",
    )
    op.drop_index(
        "ix_provider_event_blob_audit_receipt_id",
        table_name="provider_event_blob_audit",
    )
    op.drop_index(
        "ix_provider_event_blob_audit_binding_id",
        table_name="provider_event_blob_audit",
    )
    op.drop_index(
        "ix_provider_event_blob_audit_blob_id",
        table_name="provider_event_blob_audit",
    )
    op.drop_index(
        "ix_provider_event_blob_audit_action",
        table_name="provider_event_blob_audit",
    )
    op.drop_table("provider_event_blob_audit")
    op.drop_index(
        "ix_provider_event_blob_orphan_sweep",
        table_name="provider_event_blobs",
    )
    op.drop_index(
        "ix_provider_event_blobs_commit_state",
        table_name="provider_event_blobs",
    )
    op.drop_index(
        "ix_provider_event_blobs_receipt_id",
        table_name="provider_event_blobs",
    )
    op.drop_index(
        "ix_provider_event_blobs_binding_id",
        table_name="provider_event_blobs",
    )
    op.drop_index(
        "ix_provider_event_blobs_namespace_key",
        table_name="provider_event_blobs",
    )
    op.drop_index(
        "ix_provider_event_blobs_blob_id",
        table_name="provider_event_blobs",
    )
    op.drop_table("provider_event_blobs")
