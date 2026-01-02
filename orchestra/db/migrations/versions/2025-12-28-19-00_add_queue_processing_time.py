"""Add processing_started_at to embedding_queue for accurate stale detection

This migration adds a timestamp column to track when an item was claimed
for processing (not when it was created). This enables accurate stale
detection for crash recovery without incorrectly resetting in-flight items.

Revision ID: add_queue_processing_time
Revises: add_assistant_secrets
Create Date: 2025-12-28 19:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_queue_processing_time"
down_revision = "add_assistant_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add processing_started_at column to embedding_queue table.

    This column tracks when an item was claimed for processing, enabling
    accurate stale detection. Without this, we can only use created_at
    which incorrectly marks old-but-just-claimed items as stale.
    """
    # Add the processing_started_at column
    op.add_column(
        "embedding_queue",
        sa.Column("processing_started_at", sa.TIMESTAMP(), nullable=True),
    )

    # Add index for efficient stale processing detection queries
    # This index covers: WHERE status = 'processing' AND processing_started_at < ...
    op.create_index(
        "idx_embedding_queue_processing_started",
        "embedding_queue",
        ["status", "processing_started_at"],
        unique=False,
    )


def downgrade() -> None:
    """
    Remove processing_started_at column and its index.
    """
    op.drop_index(
        "idx_embedding_queue_processing_started",
        table_name="embedding_queue",
    )
    op.drop_column("embedding_queue", "processing_started_at")
