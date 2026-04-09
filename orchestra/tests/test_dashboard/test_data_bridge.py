"""Tile bridge integration tests.

These tests create real projects and logs, then exercise the bridge
endpoints end-to-end — no mocks.  Covers the filter bridge, reduce
bridge, join bridge, and join-reduce bridge.
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user

from .conftest import token_body


async def _seed_project_with_logs(client: AsyncClient, user: dict, project_name: str):
    """Create a project + a few log entries and return the log event IDs."""
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=user["headers"],
    )

    entries = [
        {"model": "gpt-4", "latency": 0.45, "cost": 0.003},
        {"model": "gpt-4", "latency": 0.52, "cost": 0.004},
        {"model": "claude-3", "latency": 0.30, "cost": 0.002},
    ]
    resp = await client.post(
        "/v0/logs",
        json={"project_name": project_name, "entries": entries},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.json()
    return resp.json().get("log_event_ids", [])


async def _seed_two_contexts(client: AsyncClient, user: dict, project_name: str):
    """Create a project with two contexts for join testing."""
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=user["headers"],
    )

    orders = [
        {"user_id": 1, "amount": 10},
        {"user_id": 2, "amount": 20},
        {"user_id": 1, "amount": 30},
    ]
    users = [
        {"user_id": 1, "city": "NYC"},
        {"user_id": 2, "city": "LA"},
    ]

    for entry in orders:
        resp = await client.post(
            "/v0/logs",
            json={
                "project_name": project_name,
                "context": "orders",
                "entries": entry,
            },
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text

    for entry in users:
        resp = await client.post(
            "/v0/logs",
            json={
                "project_name": project_name,
                "context": "users",
                "entries": entry,
            },
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.anyio
async def test_filter_bridge_returns_flattened_rows(
    client: AsyncClient,
    dbsession: Session,
):
    """Filter bridge returns entries from real logs, flattened into rows."""
    user = await create_test_user(client, "bridge_ok@test.com")
    await _seed_project_with_logs(client, user, "bridge-ok-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "bridge_ok_01",
            "tile",
            "bridge-ok-proj/Dashboards/Tiles",
            "bridge-ok-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/bridge_ok_01/filter",
        json={"context": ""},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["total_count"] == 3
    assert len(data["rows"]) == 3

    models = {row["model"] for row in data["rows"]}
    assert "gpt-4" in models
    assert "claude-3" in models


@pytest.mark.anyio
async def test_filter_bridge_respects_limit(
    client: AsyncClient,
    dbsession: Session,
):
    """Filter bridge passes limit through to the log query."""
    user = await create_test_user(client, "bridge_limit@test.com")
    await _seed_project_with_logs(client, user, "bridge-limit-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "bridge_lim01",
            "tile",
            "bridge-limit-proj/Dashboards/Tiles",
            "bridge-limit-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/bridge_lim01/filter",
        json={"context": "", "limit": 1},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert len(data["rows"]) == 1
    assert data["total_count"] == 3


@pytest.mark.anyio
async def test_filter_bridge_respects_filter_expr(
    client: AsyncClient,
    dbsession: Session,
):
    """Filter bridge forwards filter_expr so only matching logs are returned."""
    user = await create_test_user(client, "bridge_filter@test.com")
    await _seed_project_with_logs(client, user, "bridge-filter-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "bridge_flt01",
            "tile",
            "bridge-filter-proj/Dashboards/Tiles",
            "bridge-filter-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/bridge_flt01/filter",
        json={"context": "", "filter_expr": "model == 'claude-3'"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["total_count"] == 1
    assert len(data["rows"]) == 1
    assert data["rows"][0]["model"] == "claude-3"


@pytest.mark.anyio
async def test_filter_bridge_empty_project_returns_empty(
    client: AsyncClient,
    dbsession: Session,
):
    """Filter bridge returns zero rows for a project with no logs."""
    user = await create_test_user(client, "bridge_empty@test.com")

    await client.post(
        "/v0/project",
        json={"name": "bridge-empty-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "bridge_emp01",
            "tile",
            "bridge-empty-proj/Dashboards/Tiles",
            "bridge-empty-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/bridge_emp01/filter",
        json={"context": ""},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["total_count"] == 0
    assert data["rows"] == []


# ===========================================================================
# Creator identity scoping
# ===========================================================================


@pytest.mark.anyio
async def test_filter_bridge_uses_creator_identity(
    client: AsyncClient,
    dbsession: Session,
):
    """The filter bridge queries logs as the token creator, not the admin.
    Verify by having two users with separate projects — user A's tile
    should only see user A's project logs."""
    user_a = await create_test_user(client, "bridge_id_a@test.com")
    user_b = await create_test_user(client, "bridge_id_b@test.com")

    await _seed_project_with_logs(client, user_a, "bridge-id-a-proj")
    await _seed_project_with_logs(client, user_b, "bridge-id-b-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "bridge_ida01",
            "tile",
            "bridge-id-a-proj/Dashboards/Tiles",
            "bridge-id-a-proj",
        ),
        headers=user_a["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/bridge_ida01/filter",
        json={"context": ""},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["total_count"] == 3


@pytest.mark.anyio
async def test_filter_bridge_org_scoped_token(
    client: AsyncClient,
    dbsession: Session,
):
    """An org-scoped tile token can fetch logs from the org's project."""
    user = await create_test_user(client, "bridge_orgk@test.com")
    org = await create_test_org(client, user, "Bridge Org Key Test")

    await _seed_project_with_logs(client, org, "bridge-orgk-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "bridge_org01",
            "tile",
            "bridge-orgk-proj/Dashboards/Tiles",
            "bridge-orgk-proj",
        ),
        headers=org["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/bridge_org01/filter",
        json={"context": ""},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["total_count"] == 3


@pytest.mark.anyio
async def test_filter_bridge_console_aliases(
    client: AsyncClient,
    dbsession: Session,
):
    """Console proxy field names (filter, columns, exclude_columns) are accepted."""
    user = await create_test_user(client, "bridge_alias@test.com")
    await _seed_project_with_logs(client, user, "bridge-alias-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "brg_alias_01",
            "tile",
            "bridge-alias-proj/Dashboards/Tiles",
            "bridge-alias-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/brg_alias_01/filter",
        json={"context": "", "filter": "model == 'claude-3'"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["total_count"] == 1
    assert data["rows"][0]["model"] == "claude-3"


# ===========================================================================
# Reduce bridge – happy path
# ===========================================================================


@pytest.mark.anyio
async def test_reduce_bridge_count(
    client: AsyncClient,
    dbsession: Session,
):
    """Reduce bridge returns a count of all log entries."""
    user = await create_test_user(client, "red_brg_cnt@test.com")
    await _seed_project_with_logs(client, user, "red-brg-cnt-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "red_brg_c01",
            "tile",
            "red-brg-cnt-proj/Dashboards/Tiles",
            "red-brg-cnt-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/red_brg_c01/reduce",
        json={"context": "", "metric": "count", "columns": "model"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["result"] == 3


@pytest.mark.anyio
async def test_reduce_bridge_with_filter(
    client: AsyncClient,
    dbsession: Session,
):
    """Reduce bridge respects filter_expr."""
    user = await create_test_user(client, "red_brg_flt@test.com")
    await _seed_project_with_logs(client, user, "red-brg-flt-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "red_brg_f01",
            "tile",
            "red-brg-flt-proj/Dashboards/Tiles",
            "red-brg-flt-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/red_brg_f01/reduce",
        json={
            "context": "",
            "metric": "count",
            "columns": "model",
            "filter_expr": "model == 'gpt-4'",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["result"] == 2


@pytest.mark.anyio
async def test_reduce_bridge_multi_key(
    client: AsyncClient,
    dbsession: Session,
):
    """Reduce bridge with multiple columns returns a dict."""
    user = await create_test_user(client, "red_brg_mk@test.com")
    await _seed_project_with_logs(client, user, "red-brg-mk-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "red_brg_mk1",
            "tile",
            "red-brg-mk-proj/Dashboards/Tiles",
            "red-brg-mk-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/red_brg_mk1/reduce",
        json={
            "context": "",
            "metric": "count",
            "columns": ["model", "latency"],
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    result = resp.json()["result"]
    assert isinstance(result, dict)
    assert "model" in result
    assert "latency" in result


# ===========================================================================
# Join bridge – happy path
# ===========================================================================


@pytest.mark.anyio
async def test_join_bridge_returns_rows(
    client: AsyncClient,
    dbsession: Session,
):
    """Join bridge returns flattened rows from a cross-context join."""
    user = await create_test_user(client, "jn_brg_row@test.com")
    await _seed_two_contexts(client, user, "jn-brg-row-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "jn_brg_rw01",
            "tile",
            "jn-brg-row-proj/Dashboards/Tiles",
            "jn-brg-row-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/jn_brg_rw01/join",
        json={
            "tables": ["orders", "users"],
            "join_expr": "A.user_id == B.user_id",
            "select": {
                "A.user_id": "user_id",
                "A.amount": "amount",
                "B.city": "city",
            },
            "mode": "inner",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["total_count"] == 3
    assert len(data["rows"]) == 3

    cities = {row["city"] for row in data["rows"]}
    assert "NYC" in cities
    assert "LA" in cities


@pytest.mark.anyio
async def test_join_bridge_respects_limit(
    client: AsyncClient,
    dbsession: Session,
):
    """Join bridge passes result_limit through."""
    user = await create_test_user(client, "jn_brg_lim@test.com")
    await _seed_two_contexts(client, user, "jn-brg-lim-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "jn_brg_lm01",
            "tile",
            "jn-brg-lim-proj/Dashboards/Tiles",
            "jn-brg-lim-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/jn_brg_lm01/join",
        json={
            "tables": ["orders", "users"],
            "join_expr": "A.user_id == B.user_id",
            "select": {
                "A.user_id": "user_id",
                "A.amount": "amount",
                "B.city": "city",
            },
            "result_limit": 1,
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert len(data["rows"]) == 1
    assert data["total_count"] == 3


@pytest.mark.anyio
async def test_join_bridge_with_filters(
    client: AsyncClient,
    dbsession: Session,
):
    """Join bridge supports left_where/right_where/result_where."""
    user = await create_test_user(client, "jn_brg_flt@test.com")
    await _seed_two_contexts(client, user, "jn-brg-flt-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "jn_brg_fl01",
            "tile",
            "jn-brg-flt-proj/Dashboards/Tiles",
            "jn-brg-flt-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/jn_brg_fl01/join",
        json={
            "tables": ["orders", "users"],
            "join_expr": "A.user_id == B.user_id",
            "select": {
                "A.user_id": "user_id",
                "A.amount": "amount",
                "B.city": "city",
            },
            "right_where": "city == 'NYC'",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert all(row["city"] == "NYC" for row in data["rows"])
    assert data["total_count"] == 2


# ===========================================================================
# Join-reduce bridge – happy path
# ===========================================================================


@pytest.mark.anyio
async def test_join_reduce_bridge_sum(
    client: AsyncClient,
    dbsession: Session,
):
    """Join-reduce bridge computes an aggregate over a join."""
    user = await create_test_user(client, "jr_brg_sum@test.com")
    await _seed_two_contexts(client, user, "jr-brg-sum-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "jr_brg_sm01",
            "tile",
            "jr-brg-sum-proj/Dashboards/Tiles",
            "jr-brg-sum-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/jr_brg_sm01/join-reduce",
        json={
            "tables": ["orders", "users"],
            "join_expr": "A.user_id == B.user_id",
            "select": {
                "A.user_id": "user_id",
                "A.amount": "amount",
                "B.city": "city",
            },
            "metric": "sum",
            "columns": "amount",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    result = resp.json()
    assert result is not None


@pytest.mark.anyio
async def test_join_reduce_bridge_grouped(
    client: AsyncClient,
    dbsession: Session,
):
    """Join-reduce bridge with group_by returns grouped aggregates."""
    user = await create_test_user(client, "jr_brg_grp@test.com")
    await _seed_two_contexts(client, user, "jr-brg-grp-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "jr_brg_gp01",
            "tile",
            "jr-brg-grp-proj/Dashboards/Tiles",
            "jr-brg-grp-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/jr_brg_gp01/join-reduce",
        json={
            "tables": ["orders", "users"],
            "join_expr": "A.user_id == B.user_id",
            "select": {
                "A.user_id": "user_id",
                "A.amount": "amount",
                "B.city": "city",
            },
            "metric": "count",
            "columns": "amount",
            "group_by": "city",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    result = resp.json()
    assert isinstance(result, dict)


# ===========================================================================
# Creator identity scoping for new bridges
# ===========================================================================


@pytest.mark.anyio
async def test_reduce_bridge_uses_creator_identity(
    client: AsyncClient,
    dbsession: Session,
):
    """Reduce bridge queries as the token creator."""
    user_a = await create_test_user(client, "red_id_a@test.com")
    user_b = await create_test_user(client, "red_id_b@test.com")

    await _seed_project_with_logs(client, user_a, "red-id-a-proj")
    await _seed_project_with_logs(client, user_b, "red-id-b-proj")

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "red_ida_001",
            "tile",
            "red-id-a-proj/Dashboards/Tiles",
            "red-id-a-proj",
        ),
        headers=user_a["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/red_ida_001/reduce",
        json={"context": "", "metric": "count", "columns": "model"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["result"] == 3
