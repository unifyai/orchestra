"""Add embedding table for vector storage with HNSW indices

Revision ID: afffa4a2a8e2
Revises: 9e9fb4ffebf1
Create Date: 2025-05-10 20:44:51.684809

"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision = "afffa4a2a8e2"
down_revision = "9e9fb4ffebf1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.create_table(
        "embedding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column(
            "vector",
            Vector(1536),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["ref_id"],
            ["log_event.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id", "model", "key", name="uq_embedding"),
    )
    op.create_index(
        "embedding_hnsw_cosine_idx",
        "embedding",
        ["vector"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_cosine_ops"},
    )
    op.create_index(
        "embedding_hnsw_ip_idx",
        "embedding",
        ["vector"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_ip_ops"},
    )
    op.create_index(
        "embedding_hnsw_l2_idx",
        "embedding",
        ["vector"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_l2_ops"},
    )
    op.create_index(
        "idx_embedding_ref",
        "embedding",
        ["ref_id", "model", "key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_embedding_ref", table_name="embedding")
    op.drop_index(
        "embedding_hnsw_l2_idx",
        table_name="embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_l2_ops"},
    )
    op.drop_index(
        "embedding_hnsw_ip_idx",
        table_name="embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_ip_ops"},
    )
    op.drop_index(
        "embedding_hnsw_cosine_idx",
        table_name="embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_cosine_ops"},
    )
    op.drop_table("embedding")
