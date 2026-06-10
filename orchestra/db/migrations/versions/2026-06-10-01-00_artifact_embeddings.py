"""Generic non-log artifact embeddings.

Revision ID: 2026_artifact_embeddings
Revises: 2026_provider_integrations
Create Date: 2026-06-10 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "2026_artifact_embeddings"
down_revision = "2026_provider_integrations"
branch_labels = None
depends_on = None

JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "artifact_embedding",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_text_hash", sa.String(), nullable=False),
        sa.Column("vector", Vector(), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.UniqueConstraint(
            "namespace",
            "ref_id",
            "key",
            "model",
            name="uq_artifact_embedding_ref",
        ),
        sa.CheckConstraint(
            "model <> 'text-embedding-3-small' OR vector_dims(vector) = 1536",
            name="artifact_embedding_dims_text_openai_chk",
        ),
    )
    for column_name in ["namespace", "ref_id", "key", "model"]:
        op.create_index(
            f"ix_artifact_embedding_{column_name}",
            "artifact_embedding",
            [column_name],
        )
    op.create_index(
        "idx_artifact_embedding_lookup",
        "artifact_embedding",
        ["namespace", "ref_id", "key", "model"],
    )
    op.create_index(
        "idx_artifact_embedding_active",
        "artifact_embedding",
        ["namespace", "key", "model", "is_deleted"],
    )
    op.create_index(
        "artifact_embedding_hnsw_cosine_openai_1536_idx",
        "artifact_embedding",
        [sa.text("(vector::vector(1536)) vector_cosine_ops")],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_where=sa.text(
            "model = 'text-embedding-3-small' AND is_deleted = false",
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "artifact_embedding_hnsw_cosine_openai_1536_idx",
        table_name="artifact_embedding",
    )
    op.drop_index("idx_artifact_embedding_active", table_name="artifact_embedding")
    op.drop_index("idx_artifact_embedding_lookup", table_name="artifact_embedding")
    for column_name in ["model", "key", "ref_id", "namespace"]:
        op.drop_index(
            f"ix_artifact_embedding_{column_name}",
            table_name="artifact_embedding",
        )
    op.drop_table("artifact_embedding")
