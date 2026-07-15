"""Add org-chat attachments and human call sessions.

Revision ID: org_chat_real_chat_parity
Revises: assistant_external_ips
Create Date: 2026-07-29 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "org_chat_real_chat_parity"
down_revision = "assistant_external_ips"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dm_message",
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.create_table(
        "human_call_session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("caller_user_id", sa.String(), nullable=False),
        sa.Column("callee_user_id", sa.String(), nullable=False),
        sa.Column("livekit_room", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ringing"),
        sa.Column("thread_id", sa.Integer(), nullable=True),
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
        sa.Column("answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ringing', 'active', 'ended', 'declined')",
            name="ck_human_call_session_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["caller_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["callee_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["dm_thread.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_human_call_session_org_callee_status",
        "human_call_session",
        ["organization_id", "callee_user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_human_call_session_org_callee_status",
        table_name="human_call_session",
    )
    op.drop_table("human_call_session")
    op.drop_column("dm_message", "attachments")
