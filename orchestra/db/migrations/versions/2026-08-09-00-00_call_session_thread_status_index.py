"""Index call_session by (thread_id, status).

Backs thread-scoped, session-derived call listings (duration-only call
pills in human chat timelines): ``GET /chat/threads/{thread_id}/calls``
filters ``thread_id`` + ``status='ended'`` and orders by ``ended_at``.

Revision ID: call_session_thread_idx
Revises: rename_pe_revision_cols
Create Date: 2026-08-09 00:00:00.000000
"""

from alembic import op

revision = "call_session_thread_idx"
down_revision = "rename_pe_revision_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_call_session_thread_status",
        "call_session",
        ["thread_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_call_session_thread_status", table_name="call_session")
