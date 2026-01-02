"""Add embedding_queue table for async embedding generation

This migration creates a queue table for asynchronous embedding generation.
Embeddings are queued during log creation and processed by background workers,
decoupling log creation from OpenAI API calls and HNSW index updates.

Revision ID: add_embedding_queue
Revises: add_soft_delete_to_embeddings
Create Date: 2025-12-17 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_embedding_queue"
down_revision = "add_soft_delete_to_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create embedding_queue table for async embedding generation.
    """
    op.create_table(
        "embedding_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ref_id"],
            ["log_event.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id", "key", "model", name="uq_embedding_queue"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="chk_embedding_queue_status",
        ),
    )

    # Indexes for efficient queue processing
    op.create_index(
        "idx_embedding_queue_status_created",
        "embedding_queue",
        ["status", "created_at"],
    )
    op.create_index(
        "idx_embedding_queue_ref_id",
        "embedding_queue",
        ["ref_id"],
    )


def downgrade() -> None:
    """
    Drop embedding_queue table.
    """
    op.drop_index("idx_embedding_queue_ref_id", table_name="embedding_queue")
    op.drop_index("idx_embedding_queue_status_created", table_name="embedding_queue")
    op.drop_table("embedding_queue")
