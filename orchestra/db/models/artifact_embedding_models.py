"""Reusable non-log artifact embeddings.

This table mirrors the vector mechanics of the log-backed ``embedding`` table
without tying rows to ``log_event``. Artifact owners provide stable
``namespace``/``ref_id`` pairs; the generic DAO owns vector persistence and
similarity lookup for any non-log artifact catalog.
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from orchestra.db.base import Base

JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


class ArtifactEmbedding(Base):
    """Embedding for any platform artifact outside log-backed contexts."""

    __tablename__ = "artifact_embedding"

    id = Column(Integer, primary_key=True)
    namespace = Column(String, nullable=False, index=True)
    ref_id = Column(String, nullable=False, index=True)
    key = Column(String, nullable=False, index=True)
    model = Column(String, nullable=False, index=True)
    source_text = Column(Text, nullable=False)
    source_text_hash = Column(String, nullable=False)
    vector = Column(Vector(), nullable=False)
    metadata_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_OBJECT)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    is_deleted = Column(Boolean, nullable=False, server_default=sa.text("false"))

    __table_args__ = (
        UniqueConstraint(
            "namespace",
            "ref_id",
            "key",
            "model",
            name="uq_artifact_embedding_ref",
        ),
        Index(
            "idx_artifact_embedding_lookup",
            "namespace",
            "ref_id",
            "key",
            "model",
        ),
        Index(
            "idx_artifact_embedding_active",
            "namespace",
            "key",
            "model",
            "is_deleted",
        ),
        sa.CheckConstraint(
            "model <> 'text-embedding-3-small' OR vector_dims(vector) = 1536",
            name="artifact_embedding_dims_text_openai_chk",
        ),
        Index(
            "artifact_embedding_hnsw_cosine_openai_1536_idx",
            sa.text("(vector::vector(1536)) vector_cosine_ops"),
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=sa.text(
                "model = 'text-embedding-3-small' AND is_deleted = false",
            ),
        ),
    )
