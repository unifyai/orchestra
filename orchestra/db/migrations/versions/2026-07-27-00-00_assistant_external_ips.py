"""Add assistant-owned external-IP lifecycle records.

Revision ID: assistant_external_ips
Revises: system_assistant_jobs_project
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "assistant_external_ips"
down_revision = "system_assistant_jobs_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_external_ips",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("gcp_address_name", sa.String(), nullable=True),
        sa.Column("gcp_address_id", sa.String(), nullable=True),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("active_operation", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("retained_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "gcp_address_name",
            name="uq_assistant_external_ips_gcp_address_name",
        ),
        sa.UniqueConstraint(
            "gcp_address_id",
            name="uq_assistant_external_ips_gcp_address_id",
        ),
    )
    op.create_index(
        "ix_assistant_external_ips_assistant_id",
        "assistant_external_ips",
        ["assistant_id"],
        unique=True,
    )
    op.create_table(
        "assistant_external_ip_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("external_ip_id", sa.BigInteger(), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["external_ip_id"],
            ["assistant_external_ips.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_assistant_external_ip_history_external_ip_id",
        "assistant_external_ip_history",
        ["external_ip_id"],
    )
    op.create_index(
        "ix_assistant_external_ip_history_assistant_id",
        "assistant_external_ip_history",
        ["assistant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_external_ip_history_assistant_id",
        table_name="assistant_external_ip_history",
    )
    op.drop_index(
        "ix_assistant_external_ip_history_external_ip_id",
        table_name="assistant_external_ip_history",
    )
    op.drop_table("assistant_external_ip_history")
    op.drop_index(
        "ix_assistant_external_ips_assistant_id",
        table_name="assistant_external_ips",
    )
    op.drop_table("assistant_external_ips")
