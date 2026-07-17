"""Unified chat store: chat_thread / chat_message / call_utterance.

One Postgres-backed conversation store for every chat kind (human DM,
assistant DM, team, group) plus first-class call-transcript utterances.

Revision ID: unified_chat_store
Revises: dm_message_reactions
Create Date: 2026-08-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "unified_chat_store"
down_revision = "dm_message_reactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_thread",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "user_a_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_b_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "team_id",
            sa.Integer(),
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("chat_group.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('dm', 'assistant_dm', 'team', 'group')",
            name="ck_chat_thread_kind",
        ),
        sa.CheckConstraint(
            "user_a_id IS NULL OR user_b_id IS NULL OR user_a_id < user_b_id",
            name="ck_chat_thread_normalized_pair",
        ),
    )
    op.create_index(
        "uq_chat_thread_dm_pair",
        "chat_thread",
        ["organization_id", "user_a_id", "user_b_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'dm'"),
    )
    op.create_index(
        "uq_chat_thread_assistant_dm",
        "chat_thread",
        ["assistant_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'assistant_dm'"),
    )
    op.create_index(
        "uq_chat_thread_team",
        "chat_thread",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'team'"),
    )
    op.create_index(
        "uq_chat_thread_group",
        "chat_thread",
        ["group_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'group'"),
    )

    op.create_table(
        "chat_message",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "thread_id",
            sa.Integer(),
            sa.ForeignKey("chat_thread.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sender_user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sender_assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sender_name", sa.String(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "mentions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "reactions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_chat_message_thread_id_id",
        "chat_message",
        ["thread_id", "id"],
    )

    op.create_table(
        "call_utterance",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(), nullable=False),
        sa.Column(
            "reporter_assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "speaker_user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "speaker_assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("speaker_name", sa.String(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "spoken_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_call_utterance_call_reporter_id",
        "call_utterance",
        ["call_id", "reporter_assistant_id", "id"],
    )

    op.add_column(
        "org_call_session",
        sa.Column(
            "thread_id",
            sa.Integer(),
            sa.ForeignKey(
                "chat_thread.id",
                ondelete="SET NULL",
                name="fk_org_call_session_thread_id",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("org_call_session", "thread_id")
    op.drop_index(
        "ix_call_utterance_call_reporter_id",
        table_name="call_utterance",
    )
    op.drop_table("call_utterance")
    op.drop_index("ix_chat_message_thread_id_id", table_name="chat_message")
    op.drop_table("chat_message")
    op.drop_index("uq_chat_thread_group", table_name="chat_thread")
    op.drop_index("uq_chat_thread_team", table_name="chat_thread")
    op.drop_index("uq_chat_thread_assistant_dm", table_name="chat_thread")
    op.drop_index("uq_chat_thread_dm_pair", table_name="chat_thread")
    op.drop_table("chat_thread")
