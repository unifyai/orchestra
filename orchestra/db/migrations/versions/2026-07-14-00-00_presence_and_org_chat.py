"""Human presence heartbeats and org chat (DM) tables.

* ``user_presence`` — one row per user; ``last_seen_at`` is upserted by the
  Console heartbeat endpoint. A user is online when the timestamp is within
  the presence threshold of now.

* ``dm_thread`` / ``dm_message`` — direct human-to-human conversations inside
  one organization. The user pair on a thread is stored normalized
  (``user_a_id < user_b_id``) so exactly one thread exists per pair per org.

Team group-chat messages are NOT stored here: they live in the log-backed
``Teams/{team_id}/GroupChat`` context (owner_scope=team) so they share the
team's shared-memory lifecycle and are readable by team assistants.

Revision ID: presence_and_org_chat
Revises: assistant_default_model
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "presence_and_org_chat"
down_revision = "assistant_default_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_presence",
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "dm_thread",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_a_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_b_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "user_a_id",
            "user_b_id",
            name="uq_dm_thread_org_pair",
        ),
        sa.CheckConstraint(
            "user_a_id < user_b_id",
            name="ck_dm_thread_normalized_pair",
        ),
    )

    op.create_table(
        "dm_message",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "thread_id",
            sa.Integer(),
            sa.ForeignKey("dm_thread.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sender_user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_dm_message_thread_id_id",
        "dm_message",
        ["thread_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dm_message_thread_id_id", table_name="dm_message")
    op.drop_table("dm_message")
    op.drop_table("dm_thread")
    op.drop_table("user_presence")
