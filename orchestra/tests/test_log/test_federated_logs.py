import pytest
from httpx import AsyncClient

from . import HEADERS, HEADERS_2, _create_log, _create_project

PROJECT = "federated-logs-project"


async def _seed(client: AsyncClient, project_name: str = PROJECT):
    """Two contexts with overlapping shape plus one row missing the sort key."""
    result = await _create_project(client, project_name)
    assert result.status_code in (200, 201), result.text

    ctx_a = [
        {"name": "alpha", "score": 30, "contact_id": "1"},
        {"name": "bravo", "score": 10, "contact_id": "2"},
    ]
    ctx_b = [
        {"name": "charlie", "score": 20, "contact_id": "3"},
        {"name": "delta", "contact_id": "2"},  # no score → missing sort key
    ]
    for entries in ctx_a:
        result = await _create_log(
            client,
            project_name,
            entries=dict(entries),
            context="rootA/Contacts",
        )
        assert result.status_code in (200, 201), result.text
    for entries in ctx_b:
        result = await _create_log(
            client,
            project_name,
            entries=dict(entries),
            context="rootB/Contacts",
        )
        assert result.status_code in (200, 201), result.text


def _spec(context: str, **extra) -> dict:
    return {"context": context, **extra}


async def _federated(client: AsyncClient, payload: dict, headers=HEADERS):
    return await client.post("/v0/logs/federated", json=payload, headers=headers)


@pytest.mark.anyio
async def test_federated_union_preserves_source_order(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [
                _spec("rootA/Contacts", source="personal"),
                _spec("rootB/Contacts", source="team"),
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 4
    assert body["counts"] == {"personal": 2, "team": 2}
    sources = [log["entries"]["_federated_source"] for log in body["logs"]]
    assert sources == ["personal", "personal", "team", "team"]
    contexts = {log["entries"]["_federated_context"] for log in body["logs"]}
    assert contexts == {"rootA/Contacts", "rootB/Contacts"}


@pytest.mark.anyio
async def test_federated_global_sort_and_window(client: AsyncClient):
    await _seed(client)
    payload = {
        "project_name": PROJECT,
        "contexts": [_spec("rootA/Contacts"), _spec("rootB/Contacts")],
        "sorting": [{"field": "score", "direction": "ascending"}],
    }
    resp = await _federated(client, payload)
    assert resp.status_code == 200, resp.text
    names = [log["entries"]["name"] for log in resp.json()["logs"]]
    # Ascending by score, missing (delta) last.
    assert names == ["bravo", "charlie", "alpha", "delta"]

    # Windowed read returns the same global slice.
    resp = await _federated(client, {**payload, "offset": 1, "limit": 2})
    assert resp.status_code == 200, resp.text
    names = [log["entries"]["name"] for log in resp.json()["logs"]]
    assert names == ["charlie", "alpha"]


@pytest.mark.anyio
async def test_federated_missing_first_placement(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [_spec("rootA/Contacts"), _spec("rootB/Contacts")],
            "sorting": [
                {"field": "score", "direction": "descending", "missing": "first"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    names = [log["entries"]["name"] for log in resp.json()["logs"]]
    assert names == ["delta", "alpha", "charlie", "bravo"]


@pytest.mark.anyio
async def test_federated_dedupe_keeps_first_in_merge_order(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [
                _spec("rootA/Contacts", source="personal"),
                _spec("rootB/Contacts", source="team"),
            ],
            "unique_id_field": "contact_id",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = [
        (log["entries"]["contact_id"], log["entries"]["_federated_source"])
        for log in body["logs"]
    ]
    # contact_id 2 exists in both roots; the personal instance wins.
    # Unsorted branches arrive newest-first, matching GET /logs defaults.
    assert rows == [("2", "personal"), ("1", "personal"), ("3", "team")]
    # Counts are pre-dedupe.
    assert body["count"] == 4


@pytest.mark.anyio
async def test_federated_filters_shared_and_per_context(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [
                _spec("rootA/Contacts", filter="name != 'alpha'"),
                _spec("rootB/Contacts"),
            ],
            "filter": "exists(score)",
            "sorting": [{"field": "score"}],
        },
    )
    assert resp.status_code == 200, resp.text
    names = [log["entries"]["name"] for log in resp.json()["logs"]]
    assert names == ["bravo", "charlie"]


@pytest.mark.anyio
async def test_federated_missing_context_contributes_nothing(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [
                _spec("rootA/Contacts", source="personal"),
                _spec("rootMissing/Contacts", source="ghost"),
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    assert body["counts"] == {"personal": 2, "ghost": 0}
    assert len(body["logs"]) == 2


@pytest.mark.anyio
async def test_federated_count_only(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [_spec("rootA/Contacts"), _spec("rootB/Contacts")],
            "filter": "exists(score)",
            "limit": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["logs"] == []
    assert body["count"] == 3


@pytest.mark.anyio
async def test_federated_from_fields_per_context(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [_spec("rootA/Contacts", from_fields=["name"])],
            "annotate": False,
        },
    )
    assert resp.status_code == 200, resp.text
    for log in resp.json()["logs"]:
        assert set(log["entries"].keys()) == {"name"}


@pytest.mark.anyio
async def test_federated_unreadable_project_rejected(client: AsyncClient):
    await _seed(client)
    resp = await _federated(
        client,
        {
            "project_name": PROJECT,
            "contexts": [_spec("rootA/Contacts")],
        },
        headers=HEADERS_2,
    )
    assert resp.status_code == 404, resp.text
