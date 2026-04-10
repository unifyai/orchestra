"""Endpoint tests for the four dashboard token routes.

- POST   /v0/dashboards/tokens              (register)
- DELETE  /v0/dashboards/tokens/{token}      (delete)
- GET     /v0/admin/dashboards/tokens/{token} (admin resolve)
- POST    /v0/admin/dashboards/tiles/{token}/filter (filter bridge error cases)
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user

from .conftest import token_body

# ===========================================================================
# POST /dashboards/tokens – Token Registration
# ===========================================================================


@pytest.mark.anyio
async def test_register_token_success(client: AsyncClient, dbsession: Session):
    """Register a new token via the API."""
    user = await create_test_user(client, "reg_tok_ok@test.com")

    await client.post(
        "/v0/project",
        json={"name": "reg-tok-project"},
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "reg_tok_0001",
            "tile",
            "reg-tok-project/Dashboards/Tiles",
            "reg-tok-project",
        ),
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["token"] == "reg_tok_0001"
    assert data["entity_type"] == "tile"
    assert data["context_name"] == "reg-tok-project/Dashboards/Tiles"


@pytest.mark.anyio
async def test_register_token_conflict(client: AsyncClient, dbsession: Session):
    """Registering the same token twice returns 409 Conflict."""
    user = await create_test_user(client, "reg_tok_dup@test.com")

    await client.post(
        "/v0/project",
        json={"name": "reg-tok-dup-proj"},
        headers=user["headers"],
    )

    body = token_body(
        "dup_tok_0001",
        "tile",
        "reg-tok-dup-proj/Dashboards/Tiles",
        "reg-tok-dup-proj",
    )

    first = await client.post(
        "/v0/dashboards/tokens",
        json=body,
        headers=user["headers"],
    )
    assert first.status_code == status.HTTP_201_CREATED

    second = await client.post(
        "/v0/dashboards/tokens",
        json=body,
        headers=user["headers"],
    )
    assert second.status_code == status.HTTP_409_CONFLICT


@pytest.mark.anyio
async def test_register_token_invalid_entity_type(
    client: AsyncClient,
    dbsession: Session,
):
    """Schema validation rejects invalid entity_type."""
    user = await create_test_user(client, "reg_tok_bad@test.com")

    await client.post(
        "/v0/project",
        json={"name": "reg-tok-bad-proj"},
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/dashboards/tokens",
        json={
            "token": "bad_type_001",
            "entity_type": "widget",
            "context_name": "reg-tok-bad-proj/Dashboards/Tiles",
            "project_name": "reg-tok-bad-proj",
        },
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_register_token_project_not_found(
    client: AsyncClient,
    dbsession: Session,
):
    """Registration returns 404 when the named project doesn't exist."""
    user = await create_test_user(client, "reg_tok_noproj@test.com")

    resp = await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "noproj_0001",
            "tile",
            "nonexistent-proj/Dashboards/Tiles",
            "nonexistent-proj",
        ),
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "nonexistent-proj" in resp.json()["detail"]


@pytest.mark.anyio
async def test_register_token_missing_project_name_field(
    client: AsyncClient,
    dbsession: Session,
):
    """Omitting project_name from the request body returns 422."""
    user = await create_test_user(client, "reg_tok_nofld@test.com")

    resp = await client.post(
        "/v0/dashboards/tokens",
        json={
            "token": "nofld_00001",
            "entity_type": "tile",
            "context_name": "some-proj/Dashboards/Tiles",
        },
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_register_dashboard_token(client: AsyncClient, dbsession: Session):
    """Register a dashboard (layout) token."""
    user = await create_test_user(client, "reg_dash_tok@test.com")

    await client.post(
        "/v0/project",
        json={"name": "reg-dash-project"},
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "dash_tok_001",
            "dashboard",
            "reg-dash-project/Dashboards/Layouts",
            "reg-dash-project",
        ),
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.json()["entity_type"] == "dashboard"


# ===========================================================================
# DELETE /dashboards/tokens/{token} – Token Deletion
# ===========================================================================


@pytest.mark.anyio
async def test_delete_token_success(client: AsyncClient, dbsession: Session):
    """Owner can delete their own token."""
    user = await create_test_user(client, "del_tok_ok@test.com")

    await client.post(
        "/v0/project",
        json={"name": "del-tok-project"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "del_tok_0001",
            "tile",
            "del-tok-project/Dashboards/Tiles",
            "del-tok-project",
        ),
        headers=user["headers"],
    )

    resp = await client.delete(
        "/v0/dashboards/tokens/del_tok_0001",
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["deleted"] is True
    assert resp.json()["token"] == "del_tok_0001"


@pytest.mark.anyio
async def test_delete_token_not_found(client: AsyncClient, dbsession: Session):
    """Deleting a non-existent token returns 404."""
    user = await create_test_user(client, "del_tok_miss@test.com")

    resp = await client.delete(
        "/v0/dashboards/tokens/does_not_exist",
        headers=user["headers"],
    )

    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_delete_token_wrong_owner(client: AsyncClient, dbsession: Session):
    """A different user cannot delete someone else's token."""
    owner = await create_test_user(client, "del_tok_owner@test.com")
    other = await create_test_user(client, "del_tok_other@test.com")

    await client.post(
        "/v0/project",
        json={"name": "del-tok-own-proj"},
        headers=owner["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "own_tok_0001",
            "tile",
            "del-tok-own-proj/Dashboards/Tiles",
            "del-tok-own-proj",
        ),
        headers=owner["headers"],
    )

    resp = await client.delete(
        "/v0/dashboards/tokens/own_tok_0001",
        headers=other["headers"],
    )

    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "own" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_delete_then_reregister(client: AsyncClient, dbsession: Session):
    """After deletion the same token can be reregistered."""
    user = await create_test_user(client, "del_rereg@test.com")

    await client.post(
        "/v0/project",
        json={"name": "del-rereg-proj"},
        headers=user["headers"],
    )

    body = token_body(
        "rereg_tk_01",
        "tile",
        "del-rereg-proj/Dashboards/Tiles",
        "del-rereg-proj",
    )

    await client.post("/v0/dashboards/tokens", json=body, headers=user["headers"])
    await client.delete("/v0/dashboards/tokens/rereg_tk_01", headers=user["headers"])

    resp = await client.post(
        "/v0/dashboards/tokens",
        json=body,
        headers=user["headers"],
    )
    assert resp.status_code == status.HTTP_201_CREATED


# ===========================================================================
# GET /admin/dashboards/tokens/{token} – Admin Resolution
# ===========================================================================


@pytest.mark.anyio
async def test_admin_resolve_token(client: AsyncClient, dbsession: Session):
    """Admin can resolve a token to its context + creator identity."""
    user = await create_test_user(client, "adm_resolve@test.com")

    await client.post(
        "/v0/project",
        json={"name": "adm-resolve-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "adm_res_0001",
            "tile",
            "adm-resolve-proj/Dashboards/Tiles",
            "adm-resolve-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.get(
        "/v0/admin/dashboards/tokens/adm_res_0001",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["entity_type"] == "tile"
    assert data["context_name"] == "adm-resolve-proj/Dashboards/Tiles"
    assert data["user_id"] == user["id"]
    assert data["organization_id"] is None
    assert isinstance(data["project_id"], int)


@pytest.mark.anyio
async def test_admin_resolve_token_not_found(
    client: AsyncClient,
    dbsession: Session,
):
    """Admin resolution returns 404 for unknown token."""
    resp = await client.get(
        "/v0/admin/dashboards/tokens/no_such_token",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_admin_resolve_dashboard_token(
    client: AsyncClient,
    dbsession: Session,
):
    """Admin resolution works for dashboard (layout) tokens too."""
    user = await create_test_user(client, "adm_res_dash@test.com")

    await client.post(
        "/v0/project",
        json={"name": "adm-res-dash-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "adm_dash_001",
            "dashboard",
            "adm-res-dash-proj/Dashboards/Layouts",
            "adm-res-dash-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.get(
        "/v0/admin/dashboards/tokens/adm_dash_001",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["entity_type"] == "dashboard"


@pytest.mark.anyio
async def test_admin_resolve_with_org(client: AsyncClient, dbsession: Session):
    """Admin resolution returns organization_id when the token was org-scoped."""
    user = await create_test_user(client, "adm_res_org@test.com")
    org = await create_test_org(client, user, "Admin Resolve Org")

    await client.post(
        "/v0/project",
        json={"name": "adm-res-org-proj"},
        headers=org["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "org_res_0001",
            "tile",
            "adm-res-org-proj/Dashboards/Tiles",
            "adm-res-org-proj",
        ),
        headers=org["headers"],
    )

    resp = await client.get(
        "/v0/admin/dashboards/tokens/org_res_0001",
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["organization_id"] == org["id"]
    assert data["user_id"] == user["id"]


@pytest.mark.anyio
async def test_admin_resolve_non_admin_key_rejected(
    client: AsyncClient,
    dbsession: Session,
):
    """A regular API key cannot hit the admin resolution endpoint."""
    user = await create_test_user(client, "adm_no_access@test.com")

    resp = await client.get(
        "/v0/admin/dashboards/tokens/any_token",
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# ===========================================================================
# POST /admin/dashboards/tiles/{token}/filter – Filter Bridge (error cases)
# ===========================================================================


@pytest.mark.anyio
async def test_filter_bridge_token_not_found(client: AsyncClient, dbsession: Session):
    """Filter bridge returns 404 for unknown token."""
    resp = await client.post(
        "/v0/admin/dashboards/tiles/nonexistent1/filter",
        json={"context": "some/ctx"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_filter_bridge_rejects_dashboard_token(
    client: AsyncClient,
    dbsession: Session,
):
    """Filter bridge only works for tiles, not dashboards."""
    user = await create_test_user(client, "bridge_dash@test.com")

    await client.post(
        "/v0/project",
        json={"name": "bridge-dash-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "bridge_d_001",
            "dashboard",
            "bridge-dash-proj/Dashboards/Layouts",
            "bridge-dash-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/bridge_d_001/filter",
        json={"context": "bridge-dash-proj/Logs"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "tile" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_filter_bridge_non_admin_key_rejected(
    client: AsyncClient,
    dbsession: Session,
):
    """A regular API key cannot hit the filter bridge endpoint."""
    user = await create_test_user(client, "bridge_no_adm@test.com")

    resp = await client.post(
        "/v0/admin/dashboards/tiles/any_token/filter",
        json={"context": "some/ctx"},
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# ===========================================================================
# POST /admin/dashboards/tiles/{token}/reduce – Reduce Bridge (error cases)
# ===========================================================================


@pytest.mark.anyio
async def test_reduce_bridge_token_not_found(client: AsyncClient, dbsession: Session):
    """Reduce bridge returns 404 for unknown token."""
    resp = await client.post(
        "/v0/admin/dashboards/tiles/nonexistent1/reduce",
        json={"context": "", "metric": "count", "columns": "score"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_reduce_bridge_rejects_dashboard_token(
    client: AsyncClient,
    dbsession: Session,
):
    """Reduce bridge only works for tiles, not dashboards."""
    user = await create_test_user(client, "red_brg_dash@test.com")

    await client.post(
        "/v0/project",
        json={"name": "red-brg-dash-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "red_brg_d01",
            "dashboard",
            "red-brg-dash-proj/Dashboards/Layouts",
            "red-brg-dash-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/red_brg_d01/reduce",
        json={"context": "", "metric": "count", "columns": "score"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "tile" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_reduce_bridge_non_admin_key_rejected(
    client: AsyncClient,
    dbsession: Session,
):
    """A regular API key cannot hit the reduce bridge endpoint."""
    user = await create_test_user(client, "red_brg_noadm@test.com")

    resp = await client.post(
        "/v0/admin/dashboards/tiles/any_token/reduce",
        json={"context": "", "metric": "count", "columns": "score"},
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# ===========================================================================
# POST /admin/dashboards/tiles/{token}/join – Join Bridge (error cases)
# ===========================================================================


@pytest.mark.anyio
async def test_join_bridge_token_not_found(client: AsyncClient, dbsession: Session):
    """Join bridge returns 404 for unknown token."""
    resp = await client.post(
        "/v0/admin/dashboards/tiles/nonexistent1/join",
        json={
            "tables": ["proj/ctx_a", "proj/ctx_b"],
            "join_expr": "A.id == B.id",
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_join_bridge_rejects_dashboard_token(
    client: AsyncClient,
    dbsession: Session,
):
    """Join bridge only works for tiles, not dashboards."""
    user = await create_test_user(client, "jn_brg_dash@test.com")

    await client.post(
        "/v0/project",
        json={"name": "jn-brg-dash-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "jn_brg_d_01",
            "dashboard",
            "jn-brg-dash-proj/Dashboards/Layouts",
            "jn-brg-dash-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/jn_brg_d_01/join",
        json={
            "tables": ["proj/ctx_a", "proj/ctx_b"],
            "join_expr": "A.id == B.id",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "tile" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_join_bridge_non_admin_key_rejected(
    client: AsyncClient,
    dbsession: Session,
):
    """A regular API key cannot hit the join bridge endpoint."""
    user = await create_test_user(client, "jn_brg_noadm@test.com")

    resp = await client.post(
        "/v0/admin/dashboards/tiles/any_token/join",
        json={
            "tables": ["proj/ctx_a", "proj/ctx_b"],
            "join_expr": "A.id == B.id",
        },
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )


# ===========================================================================
# POST /admin/dashboards/tiles/{token}/join-reduce – Join-Reduce Bridge (error cases)
# ===========================================================================


@pytest.mark.anyio
async def test_join_reduce_bridge_token_not_found(
    client: AsyncClient,
    dbsession: Session,
):
    """Join-reduce bridge returns 404 for unknown token."""
    resp = await client.post(
        "/v0/admin/dashboards/tiles/nonexistent1/join-reduce",
        json={
            "tables": ["proj/ctx_a", "proj/ctx_b"],
            "join_expr": "A.id == B.id",
            "metric": "count",
            "columns": "amount",
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_join_reduce_bridge_rejects_dashboard_token(
    client: AsyncClient,
    dbsession: Session,
):
    """Join-reduce bridge only works for tiles, not dashboards."""
    user = await create_test_user(client, "jr_brg_dash@test.com")

    await client.post(
        "/v0/project",
        json={"name": "jr-brg-dash-proj"},
        headers=user["headers"],
    )

    await client.post(
        "/v0/dashboards/tokens",
        json=token_body(
            "jr_brg_d_01",
            "dashboard",
            "jr-brg-dash-proj/Dashboards/Layouts",
            "jr-brg-dash-proj",
        ),
        headers=user["headers"],
    )

    resp = await client.post(
        "/v0/admin/dashboards/tiles/jr_brg_d_01/join-reduce",
        json={
            "tables": ["proj/ctx_a", "proj/ctx_b"],
            "join_expr": "A.id == B.id",
            "metric": "count",
            "columns": "amount",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "tile" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_join_reduce_bridge_non_admin_key_rejected(
    client: AsyncClient,
    dbsession: Session,
):
    """A regular API key cannot hit the join-reduce bridge endpoint."""
    user = await create_test_user(client, "jr_brg_noadm@test.com")

    resp = await client.post(
        "/v0/admin/dashboards/tiles/any_token/join-reduce",
        json={
            "tables": ["proj/ctx_a", "proj/ctx_b"],
            "join_expr": "A.id == B.id",
            "metric": "count",
            "columns": "amount",
        },
        headers=user["headers"],
    )

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )
