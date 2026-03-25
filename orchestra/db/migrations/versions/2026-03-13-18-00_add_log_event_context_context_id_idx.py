"""Add index on log_event_context.context_id for FK cascade performance.

The log_event_context PK is (log_event_id, context_id), so lookups by
context_id alone require a sequential scan. This is triggered every time
a context is CASCADE-deleted (e.g. during project deletion), causing
full table scans that dominate deletion time.

Revision ID: add_lec_context_id_idx
Revises: add_cancelled_eq_status
Create Date: 2026-03-13 18:00:00.000000
"""

from alembic import op

revision = "add_lec_context_id_idx"
down_revision = "add_cancelled_eq_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_log_event_context_context_id",
        "log_event_context",
        ["context_id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("idx_log_event_context_context_id", table_name="log_event_context")
