"""Add 'cancelled' to embedding_queue status CHECK constraint.

The project deletion flow sets embedding_queue.status = 'cancelled' to
prevent workers from processing items for a project being deleted.
The CHECK constraint was missing this value, causing constraint violations.

Revision ID: add_cancelled_eq_status
Revises: add_desktop_filesync_sshkey
Create Date: 2026-03-13 12:00:00.000000
"""

from alembic import op

revision = "add_cancelled_eq_status"
down_revision = "add_desktop_filesync_sshkey"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("chk_embedding_queue_status", "embedding_queue", type_="check")
    op.create_check_constraint(
        "chk_embedding_queue_status",
        "embedding_queue",
        "status IN ('pending', 'generating', 'vector_ready', 'inserting',"
        " 'completed', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("chk_embedding_queue_status", "embedding_queue", type_="check")
    op.create_check_constraint(
        "chk_embedding_queue_status",
        "embedding_queue",
        "status IN ('pending', 'generating', 'vector_ready', 'inserting',"
        " 'completed', 'failed')",
    )
