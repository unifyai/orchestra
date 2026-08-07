"""
Guards against embedding churn and dropped queue work.

Three contracts pinned here:

1. A ``PUT /logs`` that rewrites a row with **identical** values must not
   re-embed the row's vectors (previously every whole-row PUT soft-deleted and
   re-embedded the context's embeddings — the primary TOAST-churn engine). A
   PUT that genuinely changes the source text must still refresh the vector.

2. Re-queueing an embedding revives ``failed`` rows and refreshes stale text
   (previously ``ON CONFLICT DO NOTHING`` made both silent no-ops).

3. One poison text in a generation batch fails only itself, not the whole
   batch; failed items rejoin the normal claim rotation after a cool-off.
"""

import datetime as dt

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from . import HEADERS, _create_derived_entry, _create_log, _create_project


async def _put_entries(client, log_id, entries):
    return await client.put(
        "/v0/logs",
        json={"logs": [log_id], "entries": entries, "overwrite": True},
        headers=HEADERS,
    )


def _count_embed_calls(monkeypatch):
    """Wrap the embeddings API call with a counter (in-process app)."""
    from orchestra.web.api.log.python2SQL import helpers

    calls = {"n": 0}
    real = helpers._get_embeddings_batch

    def counting(texts, model, dimensions=None):
        calls["n"] += 1
        return real(texts, model, dimensions)

    monkeypatch.setattr(helpers, "_get_embeddings_batch", counting)
    return calls


@pytest.mark.anyio
async def test_put_with_unchanged_source_does_not_reembed(
    client: AsyncClient,
    dbsession,
    monkeypatch,
):
    from orchestra.db.models.orchestra_models import Embedding

    project_name = "test_put_unchanged_no_reembed"
    await _create_project(client, project_name, user=1)

    response = await _create_log(
        client,
        project_name,
        entries={"content": "stable text", "note": "v1"},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    key = "_content_emb"
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        "embed({lg:content})",
        {"lg": [log_id]},
    )
    assert response.status_code == 200, response.text

    calls = _count_embed_calls(monkeypatch)

    # Whole-row PUT with the identical source value (clients do this
    # constantly) — must not re-embed.
    response = await _put_entries(
        client,
        log_id,
        {"content": "stable text", "note": "v2"},
    )
    assert response.status_code == 200, response.text
    assert calls["n"] == 0, "identical source text must not be re-embedded"

    row = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == key,
            Embedding.is_deleted == False,  # noqa: E712
        ),
    ).scalar_one_or_none()
    assert row is not None, "embedding must survive an unchanged-source PUT"

    # A real change to the source text must refresh the vector — via the
    # queue (Stage-2 upsert), never a synchronous provider call + HNSW write
    # inside the PUT request.
    from orchestra.db.models.orchestra_models import EmbeddingQueue

    response = await _put_entries(
        client,
        log_id,
        {"content": "completely different text"},
    )
    assert response.status_code == 200, response.text
    assert calls["n"] == 0, "changed text refreshes via the queue, not inline"

    queued = dbsession.execute(
        select(EmbeddingQueue).where(
            EmbeddingQueue.ref_id == log_id,
            EmbeddingQueue.key == key,
        ),
    ).scalar_one_or_none()
    assert queued is not None, "changed source text must be enqueued"
    assert queued.text == "completely different text"
    assert queued.status == "pending"

    row = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == key,
            Embedding.is_deleted == False,  # noqa: E712
        ),
    ).scalar_one_or_none()
    assert row is not None, (
        "the stale vector stays live until the queue replaces it — "
        "no soft-delete window with missing embeddings"
    )


@pytest.mark.anyio
async def test_new_rows_enqueue_for_embedding_templates(
    client: AsyncClient,
    dbsession,
):
    """POST /logs feeds the embedding queue for template-covered contexts.

    Coverage is a write-side responsibility: without this, a new row was
    invisible to semantic search until some later backfill noticed it.
    """
    from orchestra.db.models.orchestra_models import EmbeddingQueue

    project_name = "test_new_rows_enqueue"
    await _create_project(client, project_name, user=1)

    response = await _create_log(
        client,
        project_name,
        entries={"content": "seed row"},
    )
    assert response.status_code == 200
    seed_id = response.json()["log_event_ids"][0]

    key = "_content_emb"
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        "embed({lg:content})",
        {"lg": [seed_id]},
    )
    assert response.status_code == 200, response.text

    # A later plain create (no recompute_derived flag) must be enqueued.
    response = await _create_log(
        client,
        project_name,
        entries={"content": "later row"},
    )
    assert response.status_code == 200
    new_id = response.json()["log_event_ids"][0]

    queued = dbsession.execute(
        select(EmbeddingQueue).where(
            EmbeddingQueue.ref_id == new_id,
            EmbeddingQueue.key == key,
        ),
    ).scalar_one_or_none()
    assert queued is not None, "new rows must be enqueued for embedding"
    assert queued.text == "later row"


@pytest.mark.anyio
async def test_requeue_revives_failed_and_refreshes_stale_text(
    client: AsyncClient,
    dbsession,
):
    from orchestra.db.models.orchestra_models import EmbeddingQueue
    from orchestra.web.api.log.python2SQL.helpers import (
        DEFAULT_EMBEDDING_MODEL,
        _queue_embeddings_for_generation,
    )

    project_name = "test_requeue_revival"
    await _create_project(client, project_name, user=1)
    response = await _create_log(client, project_name, entries={"content": "x"})
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    key = "_content_emb"
    # Seed a terminal 'failed' row with stale text.
    _queue_embeddings_for_generation(
        dbsession,
        {log_id: "old text"},
        None,
        None,
        key,
    )
    row = dbsession.execute(
        select(EmbeddingQueue).where(EmbeddingQueue.ref_id == log_id),
    ).scalar_one()
    row.status = "failed"
    row.retry_count = 3
    row.error_message = "boom"
    dbsession.commit()

    # Re-queue with fresh text: the failed row must be revived, not ignored.
    _queue_embeddings_for_generation(
        dbsession,
        {log_id: "new text"},
        None,
        None,
        key,
    )
    dbsession.expire_all()
    row = dbsession.execute(
        select(EmbeddingQueue).where(EmbeddingQueue.ref_id == log_id),
    ).scalar_one()
    assert row.status == "pending"
    assert row.retry_count == 0
    assert row.text == "new text"
    assert row.model == DEFAULT_EMBEDDING_MODEL


def test_poison_text_fails_alone(monkeypatch):
    from orchestra.workers import embedding_generator as gen

    def fake_batch(texts, model, dimensions=None):
        if len(texts) > 1:
            raise RuntimeError("batch rejected")
        if texts[0] == "poison":
            raise RuntimeError("bad input")
        return [[0.0] * 8]

    monkeypatch.setattr(
        "orchestra.web.api.log.python2SQL.helpers._get_embeddings_batch",
        fake_batch,
    )

    items = [
        gen.PendingQueueItem(1, 11, "k", "fine one", "m", None, 0),
        gen.PendingQueueItem(2, 12, "k", "poison", "m", None, 0),
        gen.PendingQueueItem(3, 13, "k", "fine two", "m", None, 0),
    ]
    ok, failed = gen.generate_vectors_for_items(items)
    assert sorted(r.queue_item_id for r in ok) == [1, 3]
    assert [r.queue_item_id for r in failed] == [2]


@pytest.mark.anyio
async def test_failed_items_rejoin_claim_after_cooloff(
    client: AsyncClient,
    dbsession,
):
    from orchestra.db.models.orchestra_models import EmbeddingQueue
    from orchestra.web.api.log.python2SQL.helpers import (
        _queue_embeddings_for_generation,
    )
    from orchestra.workers.embedding_generator import claim_pending_batch

    project_name = "test_failed_claim_cooloff"
    await _create_project(client, project_name, user=1)
    response = await _create_log(client, project_name, entries={"content": "x"})
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    _queue_embeddings_for_generation(dbsession, {log_id: "text"}, None, None, "k1")
    row = dbsession.execute(
        select(EmbeddingQueue).where(EmbeddingQueue.ref_id == log_id),
    ).scalar_one()
    row.status = "failed"
    row.retry_count = 3
    # Still cooling off: must NOT be claimable.
    row.processing_started_at = dt.datetime.utcnow()
    dbsession.commit()

    assert claim_pending_batch(dbsession, limit=10) == []

    # Cool-off elapsed: claimable again with a reset retry budget.
    row.status = "failed"
    row.processing_started_at = dt.datetime.utcnow() - dt.timedelta(hours=1)
    dbsession.commit()

    claimed = claim_pending_batch(dbsession, limit=10)
    assert [c.ref_id for c in claimed] == [log_id]
    assert claimed[0].retry_count == 0
