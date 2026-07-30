"""Vector sort exercised with the exact request shape Unify's semantic search sends."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from orchestra.tests.test_log import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_project,
)
from orchestra.web.api.log.python2SQL import helpers as embed_helpers


@pytest.fixture(autouse=True)
def _fixed_embeddings(monkeypatch):
    """Deterministic vectors; the code under test is the query, not the model."""

    def _fake_batch(texts, model=None, dimensions=None):
        return [[float(len(t) % 7)] * 8 for t in texts]

    monkeypatch.setattr(embed_helpers, "_get_embeddings_batch", _fake_batch)


@pytest.mark.anyio
async def test_semantic_search_shape_matches_flow_request(client: AsyncClient):
    """Mirror unify.common.semantic_search's GET /logs down to each parameter.

    That caller sends a `model=` kwarg inside embed(), asks for `_sort_distance`
    via from_fields, and sets return_sort_distance — a combination no other test
    covered while every Function/Guidance/Knowledge search in the runtime
    depends on it.
    """

    project_name = "test_vector_sort_repro"
    await _create_project(client, project_name, user=1)

    context = "flows/repro/default/0/Functions/Compositional"
    log_ids = []
    for text_value in ("alpha searches files", "beta reads pdfs", "gamma sends email"):
        response = await _create_log(
            client,
            project_name,
            context=context,
            entries={"name": text_value.split()[0], "embedding_text": text_value},
        )
        assert response.status_code == 200, response.json()
        log_ids.append(response.json()["log_event_ids"][0])

    response = await _create_derived_entry(
        client,
        project_name,
        "_embedding_text_emb",
        "embed({log:embedding_text})",
        {"log": log_ids},
        context=context,
    )
    assert response.status_code == 200, response.text

    sorting = json.dumps(
        {
            "cosine(_embedding_text_emb, embed('read attached PDF identify hidden "
            "secret code inventory document', model='text-embedding-3-small'))": "ascending",
        },
    )
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": context,
            "limit": 5,
            "offset": 0,
            "return_ids_only": False,
            "column_context": "",
            "sorting": sorting,
            "from_fields": "name&embedding_text&_sort_distance",
            "group_offset": 0,
            "nested_groups": True,
            "return_sort_distance": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert logs, "vector sort returned no rows for an embedded context"
