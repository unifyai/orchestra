"""Add assistant↔assistant chat thread kind ``assistant_peer_dm``.

Revision ID: assistant_peer_dm_threads
Revises: openrouter_model_endpoints
Create Date: 2026-08-13 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "assistant_peer_dm_threads"
down_revision = "openrouter_model_endpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_thread",
        sa.Column(
            "peer_assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.drop_constraint("ck_chat_thread_kind", "chat_thread", type_="check")
    op.create_check_constraint(
        "ck_chat_thread_kind",
        "chat_thread",
        "kind IN ('dm', 'assistant_dm', 'assistant_peer_dm', 'team', 'group')",
    )
    op.create_check_constraint(
        "ck_chat_thread_normalized_assistant_pair",
        "chat_thread",
        (
            "assistant_id IS NULL OR peer_assistant_id IS NULL OR "
            "assistant_id < peer_assistant_id"
        ),
    )
    op.create_index(
        "uq_chat_thread_assistant_peer_dm",
        "chat_thread",
        ["assistant_id", "peer_assistant_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'assistant_peer_dm'"),
    )


def downgrade() -> None:
    op.drop_index("uq_chat_thread_assistant_peer_dm", table_name="chat_thread")
    op.drop_constraint(
        "ck_chat_thread_normalized_assistant_pair",
        "chat_thread",
        type_="check",
    )
    op.drop_constraint("ck_chat_thread_kind", "chat_thread", type_="check")
    op.create_check_constraint(
        "ck_chat_thread_kind",
        "chat_thread",
        "kind IN ('dm', 'assistant_dm', 'team', 'group')",
    )
    op.drop_column("chat_thread", "peer_assistant_id")
