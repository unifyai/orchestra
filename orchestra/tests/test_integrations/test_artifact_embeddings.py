"""Tests for generic non-log artifact embedding infrastructure."""

from __future__ import annotations

from sqlalchemy.orm import Session

from orchestra.artifacts import embedding_runtime as aer
from orchestra.artifacts.embedding_runtime import ArtifactEmbeddingRuntime
from orchestra.db.dao.artifact_embedding_dao import ArtifactEmbeddingDAO
from orchestra.db.models.artifact_embedding_models import ArtifactEmbedding


def _vector(axis: int = 0) -> list[float]:
    values = [0.0] * 1536
    values[axis] = 1.0
    return values


def test_artifact_embedding_upsert_is_hash_idempotent(dbsession: Session) -> None:
    runtime = ArtifactEmbeddingRuntime(key="test")

    first, changed = runtime.upsert(
        dbsession,
        namespace="docs",
        ref_id="doc-1",
        source_text="hello world",
        metadata={"kind": "doc"},
        vector=_vector(0),
    )
    second, second_changed = runtime.upsert(
        dbsession,
        namespace="docs",
        ref_id="doc-1",
        source_text="hello world",
        metadata={"kind": "doc"},
        vector=_vector(1),
    )

    assert first is not None
    assert second is first
    assert changed is True
    assert second_changed is False
    assert dbsession.query(ArtifactEmbedding).count() == 1
    assert list(second.vector) == _vector(0)


def test_artifact_embedding_runtime_batches_only_changed_texts(
    dbsession: Session,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def fake_embed_texts(texts, *, model, dimensions=None):
        calls.append(texts)
        return [_vector(index) for index, _text in enumerate(texts)]

    monkeypatch.setattr(aer, "embed_texts", fake_embed_texts)
    runtime = ArtifactEmbeddingRuntime(key="test")
    first_rows, first_count = runtime.upsert_many(
        dbsession,
        namespace="catalog",
        artifacts=[
            {"ref_id": "a", "source_text": "alpha", "metadata": {"name": "A"}},
            {"ref_id": "b", "source_text": "beta", "metadata": {"name": "B"}},
        ],
    )
    second_rows, second_count = runtime.upsert_many(
        dbsession,
        namespace="catalog",
        artifacts=[
            {"ref_id": "a", "source_text": "alpha", "metadata": {"name": "A"}},
            {"ref_id": "b", "source_text": "beta changed", "metadata": {"name": "B"}},
        ],
    )

    assert len(first_rows) == 2
    assert first_count == 2
    assert len(second_rows) == 1
    assert second_count == 1
    assert calls == [["alpha", "beta"], ["beta changed"]]


def test_artifact_embedding_dao_soft_deletes_and_scores(dbsession: Session) -> None:
    dao = ArtifactEmbeddingDAO(dbsession, key="test", model="text-embedding-3-small")
    dao.upsert_preembedded(
        namespace="catalog",
        ref_id="a",
        source_text="alpha",
        vector=_vector(0),
    )
    dao.upsert_preembedded(
        namespace="catalog",
        ref_id="b",
        source_text="beta",
        vector=_vector(1),
    )
    deleted = dao.soft_delete_stale(namespace="catalog", active_ref_ids=["a"])

    assert deleted == 1
    assert dao.get(namespace="catalog", ref_id="b").is_deleted is True
    scores = dao.search_by_vector(namespace="catalog", query_vector=_vector(0), limit=5)
    assert list(scores) == ["a"]
    assert scores["a"][0] == 1.0
