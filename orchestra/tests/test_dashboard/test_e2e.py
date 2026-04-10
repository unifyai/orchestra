"""End-to-end / integration tests for the dashboard token system.

Covers full lifecycle flows, Unity-like composition, console resolution
chain, and cross-project / cross-user / org isolation.
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user

from .conftest import token_body

# ===========================================================================
# Full lifecycle
# ===========================================================================


@pytest.mark.anyio
async def test_full_lifecycle(client: AsyncClient, dbsession: Session):
    """Register -> resolve -> delete -> verify gone."""
    user = await create_test_user(client, "lifecycle@test.com")

    await client.post(
        "/v0/project",
        json={"name": "lifecycle-proj"},
        headers=user["headers"],
    )

    reg = await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "life_tk_001",
            "tile",
            "lifecycle-proj/Dashboards/Tiles",
            "lifecycle-proj",
        ),
        headers=user["headers"],
    )
    assert reg.status_code == status.HTTP_201_CREATED

    resolve = await client.get(
        "/v0/admin/dashboards/tokens/life_tk_001",
        headers=ADMIN_HEADERS,
    )
    assert resolve.status_code == status.HTTP_200_OK
    assert resolve.json()["context_name"] == "lifecycle-proj/Dashboards/Tiles"

    delete = await client.delete(
        "/v0/dashboards/tokens/life_tk_001",
        headers=user["headers"],
    )
    assert delete.status_code == status.HTTP_200_OK

    resolve_after = await client.get(
        "/v0/admin/dashboards/tokens/life_tk_001",
        headers=ADMIN_HEADERS,
    )
    assert resolve_after.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_multiple_tokens_per_project(client: AsyncClient, dbsession: Session):
    """A single project can have many tokens (tiles + dashboards)."""
    user = await create_test_user(client, "multi_tok@test.com")

    await client.post(
        "/v0/project",
        json={"name": "multi-tok-proj"},
        headers=user["headers"],
    )

    tokens = []
    for i in range(5):
        entity = "tile" if i < 3 else "dashboard"
        ctx = "Dashboards/Tiles" if entity == "tile" else "Dashboards/Layouts"
        tok = f"multi_tk_{i:03d}"
        resp = await client.post(
            "/v0/dashboards/tokens",
            json=token_body(
                tok,
                entity,
                f"multi-tok-proj/{ctx}",
                "multi-tok-proj",
            ),
            headers=user["headers"],
        )
        assert resp.status_code == status.HTTP_201_CREATED
        tokens.append(tok)

    for tok in tokens:
        resp = await client.get(
            f"/v0/admin/dashboards/tokens/{tok}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK


# ===========================================================================
# Unity-like tile + dashboard composition flows
# ===========================================================================


@pytest.mark.anyio
async def test_unity_tile_creation_flow(client: AsyncClient, dbsession: Session):
    """Simulate what Unity does: create project, register tile token, verify
    the admin resolution returns all the fields the console needs."""
    user = await create_test_user(client, "unity_tile@test.com")

    proj_resp = await client.post(
        "/v0/project",
        json={"name": "unity-tile-proj"},
        headers=user["headers"],
    )
    assert proj_resp.status_code == 200

    reg = await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "unity_tile01",
            "tile",
            "unity-tile-proj/Dashboards/Tiles",
            "unity-tile-proj",
        ),
        headers=user["headers"],
    )
    assert reg.status_code == status.HTTP_201_CREATED

    resolved = await client.get(
        "/v0/admin/dashboards/tokens/unity_tile01",
        headers=ADMIN_HEADERS,
    )
    assert resolved.status_code == status.HTTP_200_OK
    data = resolved.json()
    assert data["entity_type"] == "tile"
    assert data["context_name"] == "unity-tile-proj/Dashboards/Tiles"
    assert data["user_id"] == user["id"]
    assert data["organization_id"] is None
    assert isinstance(data["project_id"], int) and data["project_id"] > 0


@pytest.mark.anyio
async def test_unity_dashboard_composition_flow(
    client: AsyncClient,
    dbsession: Session,
):
    """Register 3 tile tokens then 1 dashboard token referencing them.
    Verify all resolve with correct entity_types. Deleting the dashboard
    must not affect the tiles."""
    user = await create_test_user(client, "unity_compose@test.com")

    await client.post(
        "/v0/project",
        json={"name": "unity-compose-proj"},
        headers=user["headers"],
    )

    tile_tokens = []
    for i in range(3):
        tok = f"comp_tile_{i:02d}"
        resp = await client.post(
            "/v0/dashboards/tokens",
            json=token_body(
                tok,
                "tile",
                "unity-compose-proj/Dashboards/Tiles",
                "unity-compose-proj",
            ),
            headers=user["headers"],
        )
        assert resp.status_code == status.HTTP_201_CREATED
        tile_tokens.append(tok)

    dash_resp = await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "comp_dash_00",
            "dashboard",
            "unity-compose-proj/Dashboards/Layouts",
            "unity-compose-proj",
        ),
        headers=user["headers"],
    )
    assert dash_resp.status_code == status.HTTP_201_CREATED

    for tok in tile_tokens:
        r = await client.get(
            f"/v0/admin/dashboards/tokens/{tok}",
            headers=ADMIN_HEADERS,
        )
        assert r.json()["entity_type"] == "tile"

    r = await client.get(
        "/v0/admin/dashboards/tokens/comp_dash_00",
        headers=ADMIN_HEADERS,
    )
    assert r.json()["entity_type"] == "dashboard"

    await client.delete(
        "/v0/dashboards/tokens/comp_dash_00",
        headers=user["headers"],
    )

    for tok in tile_tokens:
        r = await client.get(
            f"/v0/admin/dashboards/tokens/{tok}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == status.HTTP_200_OK

    r = await client.get(
        "/v0/admin/dashboards/tokens/comp_dash_00",
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == status.HTTP_404_NOT_FOUND


# ===========================================================================
# Console resolution -> API key lookup chain
# ===========================================================================


@pytest.mark.anyio
async def test_console_resolution_chain(client: AsyncClient, dbsession: Session):
    """Simulate the console flow: resolve token -> look up creator by user_id
    -> verify the user has an API key that can access the project."""
    user = await create_test_user(client, "console_chain@test.com")

    await client.post(
        "/v0/project",
        json={"name": "console-chain-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "console_tk01",
            "tile",
            "console-chain-proj/Dashboards/Tiles",
            "console-chain-proj",
        ),
        headers=user["headers"],
    )

    resolved = await client.get(
        "/v0/admin/dashboards/tokens/console_tk01",
        headers=ADMIN_HEADERS,
    )
    assert resolved.status_code == status.HTTP_200_OK
    resolution = resolved.json()

    user_resp = await client.get(
        f"/v0/admin/user/by-user-id?user_id={resolution['user_id']}",
        headers=ADMIN_HEADERS,
    )
    assert user_resp.status_code == status.HTTP_200_OK
    creator = user_resp.json()
    assert "api_key" in creator

    proj_resp = await client.get(
        "/v0/projects",
        headers={
            "accept": "application/json",
            "Authorization": f"Bearer {creator['api_key']}",
        },
    )
    assert proj_resp.status_code == status.HTTP_200_OK
    assert "console-chain-proj" in proj_resp.json()


# ===========================================================================
# Cross-project / cross-user / org isolation
# ===========================================================================


@pytest.mark.anyio
async def test_cross_project_isolation(client: AsyncClient, dbsession: Session):
    """Tokens registered under different projects resolve to their
    respective project_ids."""
    user = await create_test_user(client, "xproj@test.com")

    await client.post(
        "/v0/project",
        json={"name": "xproj-alpha"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/project",
        json={"name": "xproj-beta"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "xproj_tk_a1",
            "tile",
            "xproj-alpha/Dashboards/Tiles",
            "xproj-alpha",
        ),
        headers=user["headers"],
    )
    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "xproj_tk_b1",
            "tile",
            "xproj-beta/Dashboards/Tiles",
            "xproj-beta",
        ),
        headers=user["headers"],
    )

    r_a = await client.get(
        "/v0/admin/dashboards/tokens/xproj_tk_a1",
        headers=ADMIN_HEADERS,
    )
    r_b = await client.get(
        "/v0/admin/dashboards/tokens/xproj_tk_b1",
        headers=ADMIN_HEADERS,
    )

    assert r_a.json()["project_id"] != r_b.json()["project_id"]
    assert r_a.json()["context_name"] == "xproj-alpha/Dashboards/Tiles"
    assert r_b.json()["context_name"] == "xproj-beta/Dashboards/Tiles"


@pytest.mark.anyio
async def test_cross_user_token_visibility(client: AsyncClient, dbsession: Session):
    """User A's tokens are not visible to user B via the user-scoped
    delete endpoint, but the admin resolution endpoint sees all tokens."""
    user_a = await create_test_user(client, "xuser_a@test.com")
    user_b = await create_test_user(client, "xuser_b@test.com")

    await client.post(
        "/v0/project",
        json={"name": "xuser-a-proj"},
        headers=user_a["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "xuser_tk_a1",
            "tile",
            "xuser-a-proj/Dashboards/Tiles",
            "xuser-a-proj",
        ),
        headers=user_a["headers"],
    )

    del_resp = await client.delete(
        "/v0/dashboards/tokens/xuser_tk_a1",
        headers=user_b["headers"],
    )
    assert del_resp.status_code == status.HTTP_403_FORBIDDEN

    admin_resp = await client.get(
        "/v0/admin/dashboards/tokens/xuser_tk_a1",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == status.HTTP_200_OK
    assert admin_resp.json()["user_id"] == user_a["id"]


@pytest.mark.anyio
async def test_org_user_cannot_register_on_other_org_project(
    client: AsyncClient,
    dbsession: Session,
):
    """A user using their personal API key cannot register tokens against
    another org's project."""
    user_a = await create_test_user(client, "orgiso_a@test.com")
    user_b = await create_test_user(client, "orgiso_b@test.com")
    org_b = await create_test_org(client, user_b, "OrgIso B Org")

    await client.post(
        "/v0/project",
        json={"name": "orgiso-b-proj"},
        headers=org_b["headers"],
    )

    resp = await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "orgiso_tk01",
            "tile",
            "orgiso-b-proj/Dashboards/Tiles",
            "orgiso-b-proj",
        ),
        headers=user_a["headers"],
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
