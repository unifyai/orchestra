"""Add communication call sessions.

Revision ID: communication_call_sessions
Revises: billing_pii_to_stripe
Create Date: 2026-06-11 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "communication_call_sessions"
down_revision = "billing_pii_to_stripe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "communication_call_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_call_sid", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("from_number", sa.String(), nullable=False),
        sa.Column("to_number", sa.String(), nullable=False),
        sa.Column("pool_number", sa.String(), nullable=True),
        sa.Column("conference_name", sa.String(), nullable=False),
        sa.Column("livekit_room", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            server_default="created",
            nullable=False,
        ),
        sa.Column("recording_url", sa.String(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_call_sid",
            name="uq_communication_call_sessions_provider_sid",
        ),
    )
    op.create_index(
        "ix_communication_call_sessions_assistant_channel",
        "communication_call_sessions",
        ["assistant_id", "channel", "created_at"],
    )
    op.create_index(
        "ix_communication_call_sessions_livekit_room",
        "communication_call_sessions",
        ["livekit_room"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_communication_call_sessions_livekit_room",
        table_name="communication_call_sessions",
    )
    op.drop_index(
        "ix_communication_call_sessions_assistant_channel",
        table_name="communication_call_sessions",
    )
    op.drop_table("communication_call_sessions")
