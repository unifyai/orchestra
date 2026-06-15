"""Runtime helper for non-log artifact embeddings.

The DAO owns persistence and search SQL. This module only coordinates embedding
model calls with hash-based change detection so any Orchestra domain can index
shared artifacts without coupling to integrations or log-backed contexts.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from orchestra.db.dao.artifact_embedding_dao import (
    ArtifactEmbeddingDAO,
    artifact_source_text_hash,
)
from orchestra.db.models.artifact_embedding_models import ArtifactEmbedding
from orchestra.web.api.log.python2SQL.helpers import (
    DEFAULT_EMBEDDING_MODEL,
    _get_embedding,
    _get_embeddings_batch,
)

CATALOG_SEARCH_KEY = "catalog_search"


def embed_text(
    text: str,
    *,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: Optional[int] = None,
) -> list[float]:
    """Generate a vector with the same helper used by log embeddings."""

    return _get_embedding(text, model=model, dimensions=dimensions)


def embed_texts(
    texts: list[str],
    *,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: Optional[int] = None,
) -> list[list[float]]:
    """Generate batched text vectors with the same helper used by log embeddings."""

    return _get_embeddings_batch(texts, model=model, dimensions=dimensions)


class ArtifactEmbeddingRuntime:
    """Idempotent writer and scorer for text-only generic artifact embeddings."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        key: str = CATALOG_SEARCH_KEY,
        dimensions: Optional[int] = None,
    ) -> None:
        if model != DEFAULT_EMBEDDING_MODEL:
            raise ValueError(
                "ArtifactEmbedding is restricted to text-embedding-3-small.",
            )
        self.model = model
        self.key = key
        self.dimensions = dimensions

    def dao(self, session: Session) -> ArtifactEmbeddingDAO:
        return ArtifactEmbeddingDAO(session, key=self.key, model=self.model)

    def upsert(
        self,
        session: Session,
        *,
        namespace: str,
        ref_id: str,
        source_text: str,
        metadata: Optional[dict[str, Any]] = None,
        vector: Optional[list[float]] = None,
    ) -> tuple[Optional[ArtifactEmbedding], bool]:
        """Create or refresh one embedding row if the canonical source changed."""

        dao = self.dao(session)
        digest = artifact_source_text_hash(source_text)
        row = dao.get(namespace=namespace, ref_id=ref_id)
        if row and row.source_text_hash == digest and not row.is_deleted:
            return row, False

        try:
            embedding_vector = (
                vector
                if vector is not None
                else embed_text(
                    source_text,
                    model=self.model,
                    dimensions=self.dimensions,
                )
            )
        except Exception:
            return row, False

        return (
            dao.upsert_preembedded(
                namespace=namespace,
                ref_id=ref_id,
                source_text=source_text,
                metadata=metadata,
                vector=embedding_vector,
            ),
            True,
        )

    def upsert_many(
        self,
        session: Session,
        *,
        namespace: str,
        artifacts: Iterable[dict[str, Any]],
        soft_delete_stale: bool = False,
    ) -> tuple[list[ArtifactEmbedding], int]:
        """Batch-index changed artifact texts and optionally soft-delete stale refs."""

        ordered = [
            {
                "ref_id": str(item["ref_id"]),
                "source_text": str(item["source_text"]),
                "metadata": item.get("metadata") or {},
                "digest": artifact_source_text_hash(str(item["source_text"])),
            }
            for item in artifacts
            if item.get("ref_id") and item.get("source_text")
        ]
        if not ordered:
            return [], 0

        dao = self.dao(session)
        ref_ids = [item["ref_id"] for item in ordered]
        existing = dao.list_by_refs(namespace=namespace, ref_ids=ref_ids)
        changed = [
            item
            for item in ordered
            if (
                item["ref_id"] not in existing
                or existing[item["ref_id"]].source_text_hash != item["digest"]
                or existing[item["ref_id"]].is_deleted
            )
        ]

        vectors: list[list[float]] = []
        if changed:
            try:
                vectors = embed_texts(
                    [item["source_text"] for item in changed],
                    model=self.model,
                    dimensions=self.dimensions,
                )
            except Exception:
                changed = []
                vectors = []

        rows = dao.upsert_many_preembedded(
            namespace=namespace,
            artifacts=changed,
            vectors=vectors,
        )
        if soft_delete_stale:
            dao.soft_delete_stale(namespace=namespace, active_ref_ids=ref_ids)
        return rows, len(rows)

    def search(
        self,
        session: Session,
        *,
        namespace: str,
        query_text: str,
        ref_ids: Optional[Iterable[str]] = None,
        limit: int = 100,
    ) -> dict[str, tuple[float, str]]:
        """Rank artifact refs by cosine similarity, falling back safely on errors."""

        if not query_text.strip():
            return {}
        try:
            query_vector = embed_text(
                query_text,
                model=self.model,
                dimensions=self.dimensions,
            )
        except Exception:
            return {}
        return self.dao(session).search_by_vector(
            namespace=namespace,
            query_vector=query_vector,
            ref_ids=ref_ids,
            limit=limit,
        )
