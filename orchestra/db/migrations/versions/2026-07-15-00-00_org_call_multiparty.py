"""Replace 1:1 human_call_session with multi-party org_call_session.

Revision ID: org_call_multiparty
Revises: org_chat_real_chat_parity
Create Date: 2026-07-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "org_call_multiparty"
down_revision = "org_chat_real_chat_parity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_call_session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("dm_thread_id", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("livekit_room", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ringing"),
        sa.Column(
            "assistant_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('dm', 'team')", name="ck_org_call_session_scope"),
        sa.CheckConstraint(
            "status IN ('ringing', 'active', 'ended')",
            name="ck_org_call_session_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dm_thread_id"],
            ["dm_thread.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_org_call_session_org_status",
        "org_call_session",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_org_call_session_team_status",
        "org_call_session",
        ["team_id", "status"],
    )

    op.create_table(
        "org_call_participant",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("call_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="member"),
        sa.Column("status", sa.String(), nullable=False, server_default="invited"),
        sa.Column(
            "invited_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("left_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('host', 'member')",
            name="ck_org_call_participant_role",
        ),
        sa.CheckConstraint(
            "status IN ('invited', 'joined', 'declined', 'left')",
            name="ck_org_call_participant_status",
        ),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["org_call_session.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "call_id",
            "user_id",
            name="uq_org_call_participant_call_user",
        ),
    )
    op.create_index(
        "ix_org_call_participant_user_status",
        "org_call_participant",
        ["user_id", "status"],
    )

    # Migrate any existing 1:1 rows (status declined → ended).
    op.execute("""
        INSERT INTO org_call_session (
            id, organization_id, scope, dm_thread_id, team_id,
            created_by_user_id, livekit_room, status, assistant_ids,
            created_at, updated_at, answered_at, ended_at
        )
        SELECT
            id,
            organization_id,
            'dm',
            thread_id,
            NULL,
            caller_user_id,
            livekit_room,
            CASE WHEN status = 'declined' THEN 'ended' ELSE status END,
            '[]'::jsonb,
            created_at,
            updated_at,
            answered_at,
            ended_at
        FROM human_call_session
        """)
    op.execute("""
        INSERT INTO org_call_participant (
            id, call_id, user_id, role, status, invited_at, joined_at, left_at
        )
        SELECT
            id || '-host',
            id,
            caller_user_id,
            'host',
            CASE
                WHEN status IN ('active', 'ended', 'declined') THEN 'joined'
                ELSE 'joined'
            END,
            created_at,
            created_at,
            CASE WHEN status IN ('ended', 'declined') THEN ended_at ELSE NULL END
        FROM human_call_session
        """)
    op.execute("""
        INSERT INTO org_call_participant (
            id, call_id, user_id, role, status, invited_at, joined_at, left_at
        )
        SELECT
            id || '-member',
            id,
            callee_user_id,
            'member',
            CASE
                WHEN status = 'active' THEN 'joined'
                WHEN status = 'declined' THEN 'declined'
                WHEN status = 'ended' THEN 'left'
                ELSE 'invited'
            END,
            created_at,
            CASE WHEN status = 'active' THEN answered_at ELSE NULL END,
            CASE
                WHEN status IN ('ended', 'declined') THEN ended_at
                ELSE NULL
            END
        FROM human_call_session
        """)

    op.drop_index(
        "ix_human_call_session_org_callee_status",
        table_name="human_call_session",
    )
    op.drop_table("human_call_session")


def downgrade() -> None:
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
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
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

    op.drop_index(
        "ix_org_call_participant_user_status",
        table_name="org_call_participant",
    )
    op.drop_table("org_call_participant")
    op.drop_index("ix_org_call_session_team_status", table_name="org_call_session")
    op.drop_index("ix_org_call_session_org_status", table_name="org_call_session")
    op.drop_table("org_call_session")
