"""Tests for the fused ``POST /v0/logs/join_query`` endpoint.

Tests cover both **reduce mode** (with ``metric`` + ``key``) and **row mode**
(no ``metric``), plus edge-cases, error handling, and parity checks against
the legacy materialise-then-query path.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project

# ===================================================================
# Helpers
# ===================================================================


def _get_metric_val(entry: dict, metric_name: str):
    """Extract the metric value from a grouped reduce entry.

    The entry has ``{"shared_value": X, "<metric>": Y}``.
    If all raw values are identical, ``shared_value`` is set and the
    metric key is ``None``; otherwise ``shared_value`` is ``None``
    and the metric key holds the aggregate.
    """
    sv = entry.get("shared_value")
    if sv is not None:
        return sv
    return entry.get(metric_name)


@pytest.fixture()
async def seeded(client: AsyncClient):
    """Create a project with two contexts and seed rows for join tests."""
    project = "jq_test_project"
    ctx_a = "jq_orders"
    ctx_b = "jq_users"

    await _create_project(client, project)

    orders = [
        {"user_id": 1, "amount": 10, "name": "Alice"},
        {"user_id": 2, "amount": 20, "name": "Bob"},
        {"user_id": 1, "amount": 30, "name": "Alice"},
        {"user_id": 3, "amount": 40, "name": "Charlie"},
    ]
    users = [
        {"user_id": 1, "city": "NYC"},
        {"user_id": 2, "city": "LA"},
        {"user_id": 3, "city": "SF"},
    ]

    for entry in orders:
        resp = await _create_log(client, project, entries=entry, context=ctx_a)
        assert resp.status_code == 200, resp.text
    for entry in users:
        resp = await _create_log(client, project, entries=entry, context=ctx_b)
        assert resp.status_code == 200, resp.text

    yield {
        "project": project,
        "ctx_a": ctx_a,
        "ctx_b": ctx_b,
        "join_expr": "A.user_id == B.user_id",
    }


def _base_payload(seeded, **overrides) -> Dict[str, Any]:
    payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "columns": {
            "A.user_id": "user_id",
            "A.amount": "amount",
            "A.name": "name",
            "B.city": "city",
        },
    }
    payload.update(overrides)
    return payload


# ===================================================================
# Reduce-mode tests
# ===================================================================


@pytest.mark.anyio
async def test_join_query_count_grouped(client: AsyncClient, seeded):
    payload = _base_payload(seeded, metric="count", key="amount", group_by="name")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert "Alice" in result
    assert _get_metric_val(result["Alice"], "count") == 2
    assert "Bob" in result
    assert "Charlie" in result


@pytest.mark.anyio
async def test_join_query_sum_ungrouped(client: AsyncClient, seeded):
    payload = _base_payload(seeded, metric="sum", key="amount")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert float(result) == 100.0


@pytest.mark.anyio
async def test_join_query_mean_grouped(client: AsyncClient, seeded):
    payload = _base_payload(seeded, metric="mean", key="amount", group_by="city")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert "NYC" in result
    assert float(_get_metric_val(result["NYC"], "mean")) == pytest.approx(20.0)


@pytest.mark.anyio
async def test_join_query_count_with_filter(client: AsyncClient, seeded):
    payload = _base_payload(
        seeded,
        metric="count",
        key="amount",
        filter_expr="amount > 15",
    )
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert int(result) == 3


@pytest.mark.anyio
async def test_join_query_no_matches(client: AsyncClient, seeded):
    payload = _base_payload(
        seeded,
        metric="count",
        key="amount",
        filter_expr="amount > 9999",
    )
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert int(result) == 0


# ===================================================================
# Row-mode tests
# ===================================================================


@pytest.mark.anyio
async def test_join_query_rows_basic(client: AsyncClient, seeded):
    payload = _base_payload(seeded)
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    rows = data["logs"]
    assert isinstance(rows, list)
    assert len(rows) == 4


@pytest.mark.anyio
async def test_join_query_rows_with_filter(client: AsyncClient, seeded):
    payload = _base_payload(seeded, filter_expr="city == 'NYC'")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    rows = data["logs"]
    assert all(r["city"] == "NYC" for r in rows)


@pytest.mark.anyio
async def test_join_query_rows_pagination(client: AsyncClient, seeded):
    payload = _base_payload(seeded, limit=2, offset=0)
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["logs"]) == 2


@pytest.mark.anyio
async def test_join_query_rows_sorting(client: AsyncClient, seeded):
    sort_spec = json.dumps({"amount": "descending"})
    payload = _base_payload(seeded, sorting=sort_spec)
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    rows = data["logs"]
    amounts = [float(r["amount"]) for r in rows]
    assert amounts == sorted(amounts, reverse=True)


# ===================================================================
# Parity tests (fused vs. materialised-then-query)
# ===================================================================


@pytest.mark.anyio
async def test_join_query_matches_materialized_reduce(client: AsyncClient, seeded):
    join_payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "new_context": "_legacy_temp_reduce",
        "columns": list(_base_payload(seeded)["columns"].keys()),
    }
    join_resp = await client.post(
        "/v0/logs/join",
        json=join_payload,
        headers=HEADERS,
    )
    assert join_resp.status_code == 200

    legacy_resp = await client.get(
        "/v0/logs/metric/sum",
        params={
            "project_name": seeded["project"],
            "context": "_legacy_temp_reduce",
            "key": "amount",
        },
        headers=HEADERS,
    )
    assert legacy_resp.status_code == 200
    legacy_sum = legacy_resp.json()

    await client.request(
        "DELETE",
        "/v0/logs/contexts",
        json={"project_name": seeded["project"], "context": "_legacy_temp_reduce"},
        headers=HEADERS,
    )

    fused_payload = _base_payload(seeded, metric="sum", key="amount")
    fused_resp = await client.post(
        "/v0/logs/join_query",
        json=fused_payload,
        headers=HEADERS,
    )
    assert fused_resp.status_code == 200
    assert float(fused_resp.json()) == float(legacy_sum)


@pytest.mark.anyio
async def test_join_query_matches_materialized_rows(client: AsyncClient, seeded):
    join_payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "new_context": "_legacy_temp_rows",
    }
    join_resp = await client.post(
        "/v0/logs/join",
        json=join_payload,
        headers=HEADERS,
    )
    assert join_resp.status_code == 200

    legacy_resp = await client.get(
        "/v0/logs",
        params={
            "project_name": seeded["project"],
            "context": "_legacy_temp_rows",
        },
        headers=HEADERS,
    )
    assert legacy_resp.status_code == 200
    legacy_count = legacy_resp.json()["count"]

    await client.request(
        "DELETE",
        "/v0/logs/contexts",
        json={"project_name": seeded["project"], "context": "_legacy_temp_rows"},
        headers=HEADERS,
    )

    fused_payload = _base_payload(seeded)
    fused_resp = await client.post(
        "/v0/logs/join_query",
        json=fused_payload,
        headers=HEADERS,
    )
    assert fused_resp.status_code == 200
    assert fused_resp.json()["count"] == legacy_count


# ===================================================================
# Error / validation tests
# ===================================================================


@pytest.mark.anyio
async def test_join_query_invalid_metric_returns_400(client: AsyncClient, seeded):
    payload = _base_payload(seeded, metric="not_a_metric", key="amount")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_join_query_metric_without_key_returns_422(client: AsyncClient, seeded):
    """metric without key is caught by Pydantic model_validator (422)."""
    payload = _base_payload(seeded, metric="sum")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 422
    assert "key" in resp.text.lower()


@pytest.mark.anyio
async def test_join_query_key_without_metric_returns_422(client: AsyncClient, seeded):
    """key without metric is caught by Pydantic model_validator (422)."""
    payload = _base_payload(seeded, key="amount")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 422
    assert "metric" in resp.text.lower()


@pytest.mark.anyio
async def test_join_query_sorting_in_reduce_returns_422(client: AsyncClient, seeded):
    """sorting with metric is rejected by model_validator (422)."""
    payload = _base_payload(
        seeded,
        metric="sum",
        key="amount",
        sorting=json.dumps({"amount": "ascending"}),
    )
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 422
    assert "sorting" in resp.text.lower()


@pytest.mark.anyio
async def test_join_query_limit_in_reduce_returns_422(client: AsyncClient, seeded):
    """limit with metric is rejected by model_validator (422)."""
    payload = _base_payload(seeded, metric="sum", key="amount", limit=10)
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 422
    assert "limit" in resp.text.lower()


@pytest.mark.anyio
async def test_join_query_offset_in_reduce_returns_422(client: AsyncClient, seeded):
    """offset != 0 with metric is rejected by model_validator (422)."""
    payload = _base_payload(seeded, metric="sum", key="amount", offset=5)
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 422
    assert "offset" in resp.text.lower()


@pytest.mark.anyio
async def test_join_query_group_by_in_row_mode_returns_422(client: AsyncClient, seeded):
    """group_by without metric is rejected by model_validator (422)."""
    payload = _base_payload(seeded, group_by="name")
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 422
    assert "group_by" in resp.text.lower()


# ===================================================================
# Edge-case tests: columns=None, key not in columns, filtered+emb
# ===================================================================


@pytest.mark.anyio
async def test_join_query_reduce_no_columns(client: AsyncClient, seeded):
    """Reduce with columns=None falls back to merged_data path (no KeyError)."""
    payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "metric": "sum",
        "key": "amount",
    }
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert float(result) == 100.0


@pytest.mark.anyio
async def test_join_query_reduce_no_columns_grouped(client: AsyncClient, seeded):
    """Reduce grouped with columns=None uses merged_data; groups are correct."""
    payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "metric": "count",
        "key": "amount",
        "group_by": "name",
    }
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert "Alice" in result
    assert _get_metric_val(result["Alice"], "count") == 2


@pytest.mark.anyio
async def test_join_query_key_not_in_columns(client: AsyncClient, seeded):
    """When key is not in columns, the endpoint still works (key extracted from source)."""
    payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "columns": {"A.user_id": "user_id", "A.name": "name"},
        "metric": "count",
        "key": "amount",
    }
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_join_query_group_by_not_in_columns(client: AsyncClient, seeded):
    """group_by field not in columns -> all rows group under NULL key."""
    payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "columns": {"A.user_id": "user_id", "A.amount": "amount"},
        "metric": "sum",
        "key": "amount",
        "group_by": "name",
    }
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert len(result) == 1
    assert "None" in result


@pytest.mark.anyio
async def test_join_query_rows_no_columns(client: AsyncClient, seeded):
    """Row mode with columns=None returns merged rows (all fields)."""
    payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
    }
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    rows = data["logs"]
    assert isinstance(rows, list)
    assert len(rows) == 4


@pytest.mark.anyio
async def test_join_query_filtered_reduce_excludes_unused_fields(
    client: AsyncClient,
    seeded,
):
    """Filtered reduce uses from_fields that include filter-referenced fields."""
    payload = {
        "project_name": seeded["project"],
        "pair_of_args": [
            {"context": seeded["ctx_a"]},
            {"context": seeded["ctx_b"]},
        ],
        "join_expr": seeded["join_expr"],
        "mode": "inner",
        "columns": {
            "A.user_id": "user_id",
            "A.amount": "amount",
            "A.name": "name",
            "B.city": "city",
        },
        "metric": "sum",
        "key": "amount",
        "filter_expr": "city == 'NYC'",
    }
    resp = await client.post("/v0/logs/join_query", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    result = resp.json()
    assert float(result) == 40.0
