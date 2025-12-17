"""Add soft-delete mechanism to embedding table for instant deletions

Strategy:
1. Add `is_deleted` boolean column (defaults to false for existing rows)
2. Create B-tree index on is_deleted for efficient filtering
3. Drop existing HNSW indexes (CONCURRENTLY to avoid locks)
4. Recreate HNSW indexes with `AND is_deleted = false` predicate
5. Add composite index on (ref_id, is_deleted) for deletion queries

The HNSW indexes will now exclude soft-deleted rows, meaning:
- Deletion becomes an UPDATE (instant, no index surgery)
- Vector similarity searches automatically exclude deleted embeddings
- Index size decreases over time as deleted rows are excluded

Revision ID: add_soft_delete_to_embeddings
Revises: single_role_per_resource
Create Date: 2025-12-17 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_soft_delete_to_embeddings"
down_revision = "single_role_per_resource"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add soft-delete mechanism to embedding table.

    Phases:
    1. Add is_deleted column with server default
    2. Create B-tree index on is_deleted
    3. Drop existing HNSW indexes (CONCURRENTLY)
    4. Recreate HNSW indexes with soft-delete filter (CONCURRENTLY)
    5. Add composite index for deletion queries
    """
    ctx = op.get_context()

    # Phase 1: Add is_deleted column
    # Using server_default ensures existing rows are automatically marked as not deleted
    # This is instant (no table rewrite) in PostgreSQL
    op.add_column(
        "embedding",
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Phase 2: Create B-tree index on is_deleted for efficient filtering
    op.create_index(
        "idx_embedding_is_deleted",
        "embedding",
        ["is_deleted"],
    )

    # Phase 3: Drop existing HNSW indexes
    # Using CONCURRENTLY to avoid blocking production queries
    # Must be in autocommit block for CONCURRENTLY to work
    with ctx.autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_openai_1536_idx;",
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_vertexai_1408_idx;",
        )

    # Phase 4: Recreate HNSW indexes with soft-delete filter
    # The `AND is_deleted = false` predicate ensures:
    # - Only active embeddings are indexed
    # - Soft-deleted rows don't require index surgery
    # - Index size remains optimal
    with ctx.autocommit_block():

        # OpenAI text-embedding-3-small (1536 dimensions) - Cosine similarity
        # HNSW index excludes soft-deleted embeddings for performance
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_openai_1536_idx
            ON embedding USING hnsw ((vector::vector(1536)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE model = 'text-embedding-3-small' AND is_deleted = false;
            """,
        )

        # Vertex AI multimodalembedding@001 (1408 dimensions) - Cosine similarity
        # HNSW index excludes soft-deleted embeddings for performance
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_vertexai_1408_idx
            ON embedding USING hnsw ((vector::vector(1408)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE model = 'multimodalembedding@001' AND is_deleted = false;
            """,
        )

    # Phase 5: Add composite index for deletion queries
    # Optimizes queries that filter by ref_id and is_deleted (common in deletion workflows)
    op.create_index(
        "idx_embedding_ref_id_is_deleted",
        "embedding",
        ["ref_id", "is_deleted"],
    )


def downgrade() -> None:
    """
    Remove soft-delete mechanism and restore original HNSW indexes.

    WARNING: This downgrade will fail if any rows have is_deleted = true.
    You must hard-delete (or restore) soft-deleted rows before downgrading.
    """
    ctx = op.get_context()

    # Phase 1: Drop soft-delete related indexes
    op.drop_index("idx_embedding_ref_id_is_deleted", table_name="embedding")
    op.drop_index("idx_embedding_is_deleted", table_name="embedding")

    # Phase 2: Drop HNSW indexes with soft-delete filter
    with ctx.autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_openai_1536_idx;",
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_vertexai_1408_idx;",
        )

    # Phase 3: Recreate original HNSW indexes without soft-delete filter
    # These match the original schema from support_variable_vector_dims migration
    with ctx.autocommit_block():

        # OpenAI text-embedding-3-small (1536 dimensions)
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_openai_1536_idx
            ON embedding USING hnsw ((vector::vector(1536)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE model = 'text-embedding-3-small';
            """,
        )

        # Vertex AI multimodalembedding@001 (1408 dimensions)
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_vertexai_1408_idx
            ON embedding USING hnsw ((vector::vector(1408)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE model = 'multimodalembedding@001';
            """,
        )

    # Phase 4: Remove is_deleted column
    # This will fail if any rows have is_deleted = true (intentional safety check)
    op.drop_column("embedding", "is_deleted")
