"""Add durable assistant external-IP rotation operations.

Revision ID: assistant_external_ip_rotations
Revises: chat_groups
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "assistant_external_ip_rotations"
down_revision = "chat_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_external_ip_rotations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("external_ip_id", sa.BigInteger(), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="requested"),
        sa.Column("vm_name", sa.String(), nullable=True),
        sa.Column("binding_id", sa.String(), nullable=True),
        sa.Column("old_address_name", sa.String(), nullable=True),
        sa.Column("old_address", sa.String(), nullable=True),
        sa.Column("candidate_address_name", sa.String(), nullable=True),
        sa.Column("candidate_address", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("rollback_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "requested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
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
        "ix_assistant_external_ip_rotations_assistant_id",
        "assistant_external_ip_rotations",
        ["assistant_id"],
    )
    op.create_index(
        "ix_assistant_external_ip_rotations_external_ip_id",
        "assistant_external_ip_rotations",
        ["external_ip_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_external_ip_rotations_external_ip_id",
        table_name="assistant_external_ip_rotations",
    )
    op.drop_index(
        "ix_assistant_external_ip_rotations_assistant_id",
        table_name="assistant_external_ip_rotations",
    )
    op.drop_table("assistant_external_ip_rotations")
