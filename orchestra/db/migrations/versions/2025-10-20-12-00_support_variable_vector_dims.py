"""Support variable vector dimensions in embedding table with model-specific HNSW indexes

Revision ID: support_variable_vector_dims
Revises: ddaac731f82d
Create Date: 2025-10-20 12:00:00.000000
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision = "support_variable_vector_dims"
down_revision = "21edec5565a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Update the embedding table to support variable vector dimensions with model-specific indexes.

    Strategy:
    1. Drop existing global HNSW indexes (they're dimension-specific and won't work with mixed dims)
    2. Change vector column to support variable dimensions
    3. Create model-specific partial HNSW indexes for performance
       - Each model gets its own index with the correct dimension
       - Queries filter by model first, then use the appropriate index
    """
    ctx = op.get_context()

    # Step 1: Drop existing HNSW indexes that don't filter by model
    with ctx.autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_idx;")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_l2_idx;")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_ip_idx;")

    # Step 2: Alter the vector column to support variable dimensions
    op.execute(
        """
        ALTER TABLE embedding
        ALTER COLUMN vector TYPE vector USING vector::vector;
        """,
    )

    # Step 3: Add CHECK constraints to ensure dimension integrity per model
    # Use NOT VALID + VALIDATE pattern for large tables to avoid locking during initial scan
    # This prevents dimension mismatches from corrupting the indexes

    # OpenAI text embeddings constraint (1536 dimensions)
    op.execute(
        """
        ALTER TABLE embedding
        ADD CONSTRAINT embedding_dims_text_openai_chk
        CHECK (model <> 'text-embedding-3-small' OR vector_dims(vector) = 1536) NOT VALID;
        """,
    )
    # Validate the constraint (can be done while allowing writes)
    op.execute(
        """
        ALTER TABLE embedding
        VALIDATE CONSTRAINT embedding_dims_text_openai_chk;
        """,
    )

    # Vertex AI multimodal embeddings constraint (1408 dimensions)
    op.execute(
        """
        ALTER TABLE embedding
        ADD CONSTRAINT embedding_dims_vertexai_chk
        CHECK (model <> 'multimodalembedding@001' OR vector_dims(vector) = 1408) NOT VALID;
        """,
    )
    # Validate the constraint (can be done while allowing writes)
    op.execute(
        """
        ALTER TABLE embedding
        VALIDATE CONSTRAINT embedding_dims_vertexai_chk;
        """,
    )

    # Step 4: Create model-specific HNSW expression indexes with casts
    # These are partial indexes with dimension casts for optimal performance
    # The cast is critical for pgvector to know the dimension during index build
    with ctx.autocommit_block():
        # OpenAI text-embedding-3-small (1536 dimensions) - Cosine similarity
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_openai_1536_idx
            ON embedding USING hnsw ((vector::vector(1536)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE model = 'text-embedding-3-small';
            """,
        )

        # Vertex AI multimodalembedding@001 (1408 dimensions) - Cosine similarity
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_vertexai_1408_idx
            ON embedding USING hnsw ((vector::vector(1408)) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE model = 'multimodalembedding@001';
            """,
        )


def downgrade() -> None:
    """
    Revert to fixed 1536 dimensions and recreate original indexes.
    WARNING: This will fail if there are embeddings with dimensions != 1536.
    You must delete non-1536 dimensional embeddings before downgrading.
    """
    ctx = op.get_context()

    # Step 1: Drop model-specific indexes
    with ctx.autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_openai_1536_idx;")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_vertexai_1408_idx;")

    # Step 2: Drop CHECK constraints
    op.execute("ALTER TABLE embedding DROP CONSTRAINT IF EXISTS embedding_dims_text_openai_chk;")
    op.execute("ALTER TABLE embedding DROP CONSTRAINT IF EXISTS embedding_dims_vertexai_chk;")

    # Step 3: Revert to fixed 1536 dimensions
    # This will fail if there are non-1536 dimensional vectors
    op.execute(
        """
        ALTER TABLE embedding
        ALTER COLUMN vector TYPE vector(1536) USING vector::vector(1536);
        """,
    )

    # Step 4: Recreate original global indexes
    with ctx.autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_idx
            ON embedding USING hnsw (vector vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
            """,
        )

        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_l2_idx
            ON embedding USING hnsw (vector vector_l2_ops)
            WITH (m = 16, ef_construction = 64);
            """,
        )

        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_ip_idx
            ON embedding USING hnsw (vector vector_ip_ops)
            WITH (m = 16, ef_construction = 64);
            """,
        )
