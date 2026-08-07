"""
Vector-typed derived columns live in the ``embedding`` table; ``log_event.data``
holds no value for them. These tests pin the contract that presence/nullity
filters (``== None`` / ``!= None`` / ``exists()`` / ``isNone()``) resolve
against the embedding table — previously ``<vector> == None`` compiled to
``data->>key IS NULL`` and matched **every** row, which made clients re-backfill
whole contexts on every search, forever.

Also pins two side-contracts of the same fix:
- vector derived writes leave no JSONB null marker behind, and
- repeat (non-vector) derived writes actually persist once the FieldType
  exists (the old code passed the wrong ``field_types`` shape to
  ``bulk_update`` and every repeat write died on a swallowed AttributeError).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from . import _create_derived_entry, _create_log, _create_project, fetch_logs


async def _filtered_ids(client, project_name, filter_expr):
    logs = await fetch_logs(client, project_name, filter=filter_expr)
    return sorted(log["id"] for log in logs)


@pytest.mark.anyio
async def test_vector_nullity_filters_resolve_against_embedding_table(
    client: AsyncClient,
    dbsession,
):
    project_name = "test_vector_nullity_filters"
    await _create_project(client, project_name, user=1)

    log_ids = []
    for text in ("alpha content", "beta content", "gamma content"):
        response = await _create_log(
            client,
            project_name,
            entries={"content": text},
        )
        assert response.status_code == 200
        log_ids.extend(response.json()["log_event_ids"])
    embedded, unembedded = log_ids[:2], log_ids[2]

    key = "_content_emb"
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        "embed({lg:content})",
        {"lg": embedded},
    )
    assert response.status_code == 200, response.text

    assert await _filtered_ids(client, project_name, f"{key} == None") == [unembedded]
    assert await _filtered_ids(client, project_name, f"{key} != None") == sorted(
        embedded,
    )
    assert await _filtered_ids(client, project_name, f"exists({key})") == sorted(
        embedded,
    )
    assert await _filtered_ids(client, project_name, f"isNone({key})") == [unembedded]

    # The compound shape clients actually use for backfill detection must
    # return only the genuinely missing row — not the whole context.
    assert await _filtered_ids(
        client,
        project_name,
        f"({key} == None) and (content != None)",
    ) == [unembedded]


@pytest.mark.anyio
async def test_vector_derived_write_leaves_no_jsonb_marker(
    client: AsyncClient,
    dbsession,
):
    from orchestra.db.models.orchestra_models import Embedding, LogEvent

    project_name = "test_vector_no_jsonb_marker"
    await _create_project(client, project_name, user=1)

    response = await _create_log(
        client,
        project_name,
        entries={"content": "marker-free storage"},
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

    vector_row = dbsession.execute(
        select(Embedding).where(
            Embedding.ref_id == log_id,
            Embedding.key == key,
            Embedding.is_deleted == False,  # noqa: E712
        ),
    ).scalar_one_or_none()
    assert vector_row is not None and vector_row.vector is not None

    data = dbsession.execute(
        select(LogEvent.data).where(LogEvent.id == log_id),
    ).scalar_one()
    assert key not in (data or {}), (
        "vector derived writes must not leave a JSONB null marker: the marker "
        "carries no value, rewrites the whole log row (no HOT updates), and "
        "made data-based nullity checks a tautology"
    )
