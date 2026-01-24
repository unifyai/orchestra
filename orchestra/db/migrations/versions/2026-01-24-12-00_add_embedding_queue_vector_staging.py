"""Add vector staging columns to embedding_queue for decoupled pipeline

This migration adds columns to support a two-stage embedding pipeline:
- Stage 1 (parallel): Generate embeddings and store vectors in queue
- Stage 2 (serial): Bulk insert ready vectors into indexed Embedding table

New columns:
- generated_vector: Stores the embedding vector after generation
- vector_generated_at: Timestamp when vector was generated

New status values:
- generating: Being processed by Stage 1 worker
- vector_ready: Vector generated, awaiting Stage 2 insertion
- inserting: Being processed by Stage 2 worker

Revision ID: 8a3f5c2d1e9b
Revises: 1d75ec9dbdaa
Create Date: 2026-01-24 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision = "8a3f5c2d1e9b"
down_revision = "1d75ec9dbdaa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns for vector staging
    op.add_column(
        "embedding_queue",
        sa.Column("generated_vector", Vector(), nullable=True),
    )
    op.add_column(
        "embedding_queue",
        sa.Column("vector_generated_at", sa.TIMESTAMP(), nullable=True),
    )

    # Drop old status constraint
    op.drop_constraint("chk_embedding_queue_status", "embedding_queue", type_="check")

    # Create new status constraint with additional states
    op.create_check_constraint(
        "chk_embedding_queue_status",
        "embedding_queue",
        "status IN ('pending', 'generating', 'vector_ready', 'inserting', 'completed', 'failed')",
    )

    # Add index for Stage 2 worker to efficiently find vector_ready items
    op.create_index(
        "idx_embedding_queue_vector_ready",
        "embedding_queue",
        ["created_at"],
        postgresql_where=sa.text("status = 'vector_ready'"),
    )

    # Update existing 'processing' status items to 'pending' (if any exist during migration)
    op.execute(
        "UPDATE embedding_queue SET status = 'pending' WHERE status = 'processing'",
    )


def downgrade() -> None:
    # Revert any new status items back to compatible states
    op.execute(
        "UPDATE embedding_queue SET status = 'pending' WHERE status IN ('generating', 'vector_ready', 'inserting')",
    )

    # Drop new index
    op.drop_index("idx_embedding_queue_vector_ready", table_name="embedding_queue")

    # Drop new status constraint
    op.drop_constraint("chk_embedding_queue_status", "embedding_queue", type_="check")

    # Restore old status constraint
    op.create_check_constraint(
        "chk_embedding_queue_status",
        "embedding_queue",
        "status IN ('pending', 'processing', 'completed', 'failed')",
    )

    # Drop new columns
    op.drop_column("embedding_queue", "vector_generated_at")
    op.drop_column("embedding_queue", "generated_vector")
