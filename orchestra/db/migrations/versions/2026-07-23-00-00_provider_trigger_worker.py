"""Provider-trigger worker heartbeat and health failure tracking.

Revision ID: provider_trigger_worker
Revises: provider_event_blob_storage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "provider_trigger_worker"
down_revision = "provider_event_blob_storage"
branch_labels = None
depends_on = None

JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column(
        "event_trigger_bindings",
        sa.Column(
            "consecutive_health_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "provider_trigger_worker_heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("worker_key", sa.String(length=64), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("last_reconcile_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_health_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
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
            "worker_key",
            name="uq_provider_trigger_worker_heartbeats_worker_key",
        ),
    )
    op.create_index(
        "ix_provider_trigger_worker_heartbeats_worker_key",
        "provider_trigger_worker_heartbeats",
        ["worker_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_trigger_worker_heartbeats_worker_key",
        table_name="provider_trigger_worker_heartbeats",
    )
    op.drop_table("provider_trigger_worker_heartbeats")
    op.drop_column("event_trigger_bindings", "consecutive_health_failures")
