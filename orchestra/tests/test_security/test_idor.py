"""
Security tests for IDOR (Insecure Direct Object Reference) vulnerabilities.

These tests verify that User B cannot access, modify, or delete resources
belonging to User A by guessing or enumerating resource IDs.
"""

import pytest
from httpx import AsyncClient
from starlette import status

from orchestra.tests.utils import create_test_user

PROJECT_NAME = "idor-test-project"


@pytest.fixture
async def user_a(client: AsyncClient):
    return await create_test_user(client, "idor_user_a@test.com")


@pytest.fixture
async def user_b(client: AsyncClient):
    return await create_test_user(client, "idor_user_b@test.com")


@pytest.fixture
async def user_a_interface(client: AsyncClient, user_a):
    """Create a project with an interface, tab, and tile owned by User A."""
    resp = await client.post(
        "/v0/project",
        json={"name": PROJECT_NAME},
        headers=user_a["headers"],
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text

    resp = await client.post(
        "/v0/interfaces/",
        json={"name": "secret-iface", "project_name": PROJECT_NAME},
        headers=user_a["headers"],
    )
    assert resp.status_code == 201, resp.text
    iface = resp.json()

    resp = await client.post(
        "/v0/tab/",
        json={"name": "secret-tab", "interface_id": iface["id"]},
        headers=user_a["headers"],
    )
    assert resp.status_code == 201, resp.text
    tab = resp.json()

    resp = await client.post(
        "/v0/tile/",
        json={
            "name": "secret-tile",
            "tab_id": tab["id"],
            "position": {"x": 0, "y": 0, "width": 6, "height": 4},
        },
        headers=user_a["headers"],
    )
    assert resp.status_code == 201, resp.text
    tile = resp.json()

    iface["_tab"] = tab
    iface["_tile"] = tile
    return iface


# ---------------------------------------------------------------------------
# Interface IDOR
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_idor_read_interface_by_id(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to read User A's interface by ID."""
    iface_id = user_a_interface["id"]
    resp = await client.get(
        f"/v0/interfaces/?interface_id={iface_id}",
        headers=user_b["headers"],
    )
    assert (
        resp.status_code == 404
    ), f"IDOR: User B could read User A's interface (got {resp.status_code})"


@pytest.mark.anyio
async def test_idor_read_interface_by_name(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to read User A's interface by project+name."""
    resp = await client.get(
        f"/v0/interfaces/?project_name={PROJECT_NAME}&name=secret-iface",
        headers=user_b["headers"],
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_idor_update_interface_by_id(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to update User A's interface by ID."""
    iface_id = user_a_interface["id"]
    resp = await client.put(
        f"/v0/interfaces/?interface_id={iface_id}",
        json={"name": "pwned"},
        headers=user_b["headers"],
    )
    assert (
        resp.status_code == 404
    ), f"IDOR: User B could update User A's interface (got {resp.status_code})"


@pytest.mark.anyio
async def test_idor_delete_interface_by_id(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to delete User A's interface by ID."""
    iface_id = user_a_interface["id"]
    resp = await client.delete(
        f"/v0/interfaces/?interface_id={iface_id}",
        headers=user_b["headers"],
    )
    assert (
        resp.status_code == 404
    ), f"IDOR: User B could delete User A's interface (got {resp.status_code})"

    # Verify it still exists for User A
    check = await client.get(
        f"/v0/interfaces/?interface_id={iface_id}",
        headers=user_a["headers"],
    )
    assert check.status_code == 200


# ---------------------------------------------------------------------------
# Tab IDOR
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_idor_read_tab_by_id(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to read User A's tab by ID."""
    tab_id = user_a_interface["_tab"]["id"]
    resp = await client.get(
        f"/v0/tab/?tab_id={tab_id}",
        headers=user_b["headers"],
    )
    assert (
        resp.status_code == 404
    ), f"IDOR: User B could read User A's tab (got {resp.status_code})"


# ---------------------------------------------------------------------------
# Tile IDOR
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_idor_read_tile_by_id(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to read User A's tile by ID."""
    tile_id = user_a_interface["_tile"]["id"]
    resp = await client.get(
        f"/v0/tile/?tile_id={tile_id}",
        headers=user_b["headers"],
    )
    assert (
        resp.status_code == 404
    ), f"IDOR: User B could read User A's tile (got {resp.status_code})"


# ---------------------------------------------------------------------------
# Project IDOR
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_idor_read_project_by_name(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to read User A's project by name."""
    resp = await client.get(
        f"/v0/project/{PROJECT_NAME}",
        headers=user_b["headers"],
    )
    assert (
        resp.status_code == 404
    ), f"IDOR: User B could read User A's project (got {resp.status_code})"


@pytest.mark.anyio
async def test_idor_delete_project_by_name(
    client: AsyncClient,
    dbsession,
    user_a,
    user_b,
    user_a_interface,
):
    """User B must NOT be able to delete User A's project."""
    resp = await client.delete(
        f"/v0/project/{PROJECT_NAME}",
        headers=user_b["headers"],
    )
    assert (
        resp.status_code == 404
    ), f"IDOR: User B could delete User A's project (got {resp.status_code})"

    # Verify it still exists for User A
    check = await client.get(
        f"/v0/project/{PROJECT_NAME}",
        headers=user_a["headers"],
    )
    assert check.status_code == 200


# ---------------------------------------------------------------------------
# Sanity checks — owner can access own resources
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_owner_can_read_own_interface(
    client: AsyncClient,
    dbsession,
    user_a,
    user_a_interface,
):
    """User A can read their own interface by ID."""
    iface_id = user_a_interface["id"]
    resp = await client.get(
        f"/v0/interfaces/?interface_id={iface_id}",
        headers=user_a["headers"],
    )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_owner_can_read_own_project(
    client: AsyncClient,
    dbsession,
    user_a,
    user_a_interface,
):
    """User A can read their own project."""
    resp = await client.get(
        f"/v0/project/{PROJECT_NAME}",
        headers=user_a["headers"],
    )
    assert resp.status_code == 200
