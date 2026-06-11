"""Integration provider cloud bootstrap state.

Revision ID: integration_bootstrap_state
Revises: 2026_artifact_embeddings
Create Date: 2026-06-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "integration_bootstrap_state"
down_revision = "2026_artifact_embeddings"
branch_labels = None
depends_on = None

JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "integration_bootstrap_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("desired_hash", sa.String(), nullable=False),
        sa.Column(
            "desired_config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=JSON_EMPTY_OBJECT,
            nullable=False,
        ),
        sa.Column(
            "last_status",
            sa.String(),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "apps_upserted",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "tools_upserted",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "backend_id",
            name="uq_integration_bootstrap_state_env_backend",
        ),
    )
    op.create_index(
        op.f("ix_integration_bootstrap_state_environment"),
        "integration_bootstrap_state",
        ["environment"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_bootstrap_state_backend_id"),
        "integration_bootstrap_state",
        ["backend_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_bootstrap_state_last_status"),
        "integration_bootstrap_state",
        ["last_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_integration_bootstrap_state_last_status"),
        table_name="integration_bootstrap_state",
    )
    op.drop_index(
        op.f("ix_integration_bootstrap_state_backend_id"),
        table_name="integration_bootstrap_state",
    )
    op.drop_index(
        op.f("ix_integration_bootstrap_state_environment"),
        table_name="integration_bootstrap_state",
    )
    op.drop_table("integration_bootstrap_state")
