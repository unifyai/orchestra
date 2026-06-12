"""DAO for generic non-log artifact embeddings."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orchestra.db.models.artifact_embedding_models import ArtifactEmbedding


def artifact_source_text_hash(source_text: str) -> str:
    """Return the stable digest used to skip unchanged embedding writes."""

    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


class ArtifactEmbeddingDAO:
    """Persistence and vector-search boundary for generic artifact embeddings."""

    def __init__(
        self,
        session: Session,
        *,
        key: str,
        model: str,
    ) -> None:
        self.session = session
        self.key = key
        self.model = model

    def get(self, *, namespace: str, ref_id: str) -> ArtifactEmbedding | None:
        return (
            self.session.query(ArtifactEmbedding)
            .filter_by(
                namespace=namespace,
                ref_id=ref_id,
                key=self.key,
                model=self.model,
            )
            .one_or_none()
        )

    def list_by_refs(
        self,
        *,
        namespace: str,
        ref_ids: Iterable[str],
    ) -> dict[str, ArtifactEmbedding]:
        refs = list(ref_ids)
        if not refs:
            return {}
        return {
            row.ref_id: row
            for row in self.session.query(ArtifactEmbedding)
            .filter_by(namespace=namespace, key=self.key, model=self.model)
            .filter(ArtifactEmbedding.ref_id.in_(refs))
            .all()
        }

    def upsert_preembedded(
        self,
        *,
        namespace: str,
        ref_id: str,
        source_text: str,
        vector: list[float],
        metadata: Optional[dict[str, Any]] = None,
    ) -> ArtifactEmbedding:
        digest = artifact_source_text_hash(source_text)
        row = self.get(namespace=namespace, ref_id=ref_id)
        if row is None:
            row = ArtifactEmbedding(
                namespace=namespace,
                ref_id=ref_id,
                key=self.key,
                model=self.model,
                source_text=source_text,
                source_text_hash=digest,
                vector=vector,
                metadata_json=metadata or {},
                is_deleted=False,
            )
            self.session.add(row)
            self.session.flush()
            return row

        row.source_text = source_text
        row.source_text_hash = digest
        row.vector = vector
        row.metadata_json = metadata or {}
        row.is_deleted = False
        self.session.flush()
        return row

    def upsert_many_preembedded(
        self,
        *,
        namespace: str,
        artifacts: Iterable[dict[str, Any]],
        vectors: Iterable[list[float]],
    ) -> list[ArtifactEmbedding]:
        payload = [
            {
                "namespace": namespace,
                "ref_id": str(item["ref_id"]),
                "key": self.key,
                "model": self.model,
                "source_text": str(item["source_text"]),
                "source_text_hash": artifact_source_text_hash(str(item["source_text"])),
                "vector": vector,
                "metadata_json": item.get("metadata") or {},
                "is_deleted": False,
            }
            for item, vector in zip(artifacts, vectors)
            if item.get("ref_id") and item.get("source_text") and vector
        ]
        if not payload:
            return []

        table = ArtifactEmbedding.__table__
        for start in range(0, len(payload), 1000):
            chunk = payload[start : start + 1000]
            stmt = insert(table).values(chunk)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_artifact_embedding_ref",
                set_={
                    "source_text": stmt.excluded.source_text,
                    "source_text_hash": stmt.excluded.source_text_hash,
                    "vector": stmt.excluded.vector,
                    "metadata_json": stmt.excluded.metadata_json,
                    "is_deleted": False,
                    "updated_at": sa.func.now(),
                },
            )
            self.session.execute(stmt)
        self.session.flush()

        ref_ids = [item["ref_id"] for item in payload]
        return list(
            self.session.query(ArtifactEmbedding)
            .filter_by(namespace=namespace, key=self.key, model=self.model)
            .filter(ArtifactEmbedding.ref_id.in_(ref_ids))
            .all(),
        )

    def soft_delete_stale(
        self,
        *,
        namespace: str,
        active_ref_ids: Iterable[str],
    ) -> int:
        active_refs = set(active_ref_ids)
        query = self.session.query(ArtifactEmbedding).filter_by(
            namespace=namespace,
            key=self.key,
            model=self.model,
            is_deleted=False,
        )
        if active_refs:
            query = query.filter(~ArtifactEmbedding.ref_id.in_(active_refs))
        deleted = 0
        for stale in query.all():
            stale.is_deleted = True
            deleted += 1
        self.session.flush()
        return deleted

    def search_by_vector(
        self,
        *,
        namespace: str,
        query_vector: list[float],
        ref_ids: Optional[Iterable[str]] = None,
        limit: int = 100,
    ) -> dict[str, tuple[float, str]]:
        wanted_refs = set(ref_ids or [])
        try:
            sql_scores = self._search_with_pgvector(
                namespace=namespace,
                query_vector=query_vector,
                ref_ids=wanted_refs,
                limit=limit,
            )
            if sql_scores:
                return sql_scores
        except Exception:
            pass
        return self._search_in_process(
            namespace=namespace,
            query_vector=query_vector,
            ref_ids=wanted_refs,
            limit=limit,
        )

    def _base_query(self, *, namespace: str):
        return self.session.query(ArtifactEmbedding).filter_by(
            namespace=namespace,
            key=self.key,
            model=self.model,
            is_deleted=False,
        )

    def _search_with_pgvector(
        self,
        *,
        namespace: str,
        query_vector: list[float],
        ref_ids: set[str],
        limit: int,
    ) -> dict[str, tuple[float, str]]:
        dims = len(query_vector)
        distance = sa.cast(ArtifactEmbedding.vector, Vector(dims)).op("<=>")(
            query_vector,
        )
        query = self._base_query(namespace=namespace)
        if ref_ids:
            query = query.filter(ArtifactEmbedding.ref_id.in_(ref_ids))
        rows = (
            query.with_entities(ArtifactEmbedding.ref_id, distance.label("distance"))
            .order_by(distance.asc())
            .limit(limit)
            .all()
        )
        return {
            ref_id: (
                1.0 - float(distance_value),
                "embedding similarity over artifact catalog text",
            )
            for ref_id, distance_value in rows
            if distance_value is not None
        }

    def _search_in_process(
        self,
        *,
        namespace: str,
        query_vector: list[float],
        ref_ids: set[str],
        limit: int,
    ) -> dict[str, tuple[float, str]]:
        query = self._base_query(namespace=namespace)
        if ref_ids:
            query = query.filter(ArtifactEmbedding.ref_id.in_(ref_ids))
        scored: list[tuple[str, float]] = []
        for row in query.all():
            distance = _cosine_distance(query_vector, list(row.vector))
            if distance is not None:
                scored.append((row.ref_id, 1.0 - distance))
        scored.sort(key=lambda item: item[1], reverse=True)
        return {
            ref_id: (score, "embedding similarity over artifact catalog text")
            for ref_id, score in scored[:limit]
        }


def _cosine_distance(left: list[float], right: list[float]) -> Optional[float]:
    if len(left) != len(right) or not left:
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return 1.0 - (dot / (left_norm * right_norm))
