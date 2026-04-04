"""Data bridge integration tests.

These tests create real projects and logs, then exercise the data bridge
endpoint end-to-end — no mocks, no httpx. The data bridge now calls the
internal _get_logs_query / _format_logs path directly.
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user

from .conftest import token_body


async def _seed_project_with_logs(client: AsyncClient, user: dict, project_name: str):
    """Create a project + a few log entries and return the project name."""
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


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.anyio
async def test_data_bridge_returns_flattened_rows(
    client: AsyncClient,
    dbsession: Session,
):
    """Data bridge returns entries from real logs, flattened into rows."""
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
        "/v0/admin/dashboards/tiles/bridge_ok_01/data",
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
async def test_data_bridge_respects_limit(
    client: AsyncClient,
    dbsession: Session,
):
    """Data bridge passes limit through to the log query."""
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
        "/v0/admin/dashboards/tiles/bridge_lim01/data",
        json={"context": "", "limit": 1},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert len(data["rows"]) == 1
    assert data["total_count"] == 3


@pytest.mark.anyio
async def test_data_bridge_respects_filter_expr(
    client: AsyncClient,
    dbsession: Session,
):
    """Data bridge forwards filter_expr so only matching logs are returned."""
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
        "/v0/admin/dashboards/tiles/bridge_flt01/data",
        json={"context": "", "filter_expr": "model == 'claude-3'"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["total_count"] == 1
    assert len(data["rows"]) == 1
    assert data["rows"][0]["model"] == "claude-3"


@pytest.mark.anyio
async def test_data_bridge_empty_project_returns_empty(
    client: AsyncClient,
    dbsession: Session,
):
    """Data bridge returns zero rows for a project with no logs."""
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
        "/v0/admin/dashboards/tiles/bridge_emp01/data",
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
async def test_data_bridge_uses_creator_identity(
    client: AsyncClient,
    dbsession: Session,
):
    """The data bridge queries logs as the token creator, not the admin.
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
        "/v0/admin/dashboards/tiles/bridge_ida01/data",
        json={"context": ""},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["total_count"] == 3


@pytest.mark.anyio
async def test_data_bridge_org_scoped_token(
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
        "/v0/admin/dashboards/tiles/bridge_org01/data",
        json={"context": ""},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["total_count"] == 3
