from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield mock_wake_up, mock_reawaken


@pytest.fixture(scope="function", autouse=True)
async def approve_default_user(client: AsyncClient):
    from orchestra.tests.utils import ADMIN_HEADERS

    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]
    approve_url = f"/v0/admin/user/{user_id}/assistant-hiring-approval/approved"
    approve_resp = await client.put(approve_url, headers=ADMIN_HEADERS)
    assert approve_resp.status_code == status.HTTP_200_OK


# =============================================================================
# Desktop CRUD
# =============================================================================


@pytest.mark.anyio
async def test_register_desktop(client: AsyncClient):
    payload = {
        "name": "My MacBook",
        "url": "https://abc.tunnel.unify.ai",
        "os": "macos",
    }
    resp = await client.post("/v0/desktop", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()["info"]
    assert data["name"] == "My MacBook"
    assert data["url"] == "https://abc.tunnel.unify.ai"
    assert data["os"] == "macos"
    assert data["assigned_to_assistant_id"] is None
    assert "id" in data
    assert "created_at" in data


@pytest.mark.anyio
async def test_list_desktops(client: AsyncClient):
    resp_empty = await client.get("/v0/desktop", headers=HEADERS)
    assert resp_empty.status_code == 200
    assert resp_empty.json()["info"] == []

    await client.post(
        "/v0/desktop",
        json={"name": "Desktop A", "url": "https://a.tunnel.unify.ai", "os": "ubuntu"},
        headers=HEADERS,
    )
    await client.post(
        "/v0/desktop",
        json={"name": "Desktop B", "url": "https://b.tunnel.unify.ai", "os": "windows"},
        headers=HEADERS,
    )

    resp = await client.get("/v0/desktop", headers=HEADERS)
    assert resp.status_code == 200
    desktops = resp.json()["info"]
    assert len(desktops) == 2
    names = {d["name"] for d in desktops}
    assert names == {"Desktop A", "Desktop B"}


@pytest.mark.anyio
async def test_update_desktop(client: AsyncClient):
    create_resp = await client.post(
        "/v0/desktop",
        json={"name": "Old Name", "url": "https://old.tunnel.unify.ai", "os": "macos"},
        headers=HEADERS,
    )
    desktop_id = create_resp.json()["info"]["id"]

    patch_resp = await client.patch(
        f"/v0/desktop/{desktop_id}",
        json={"name": "New Name", "url": "https://new.tunnel.unify.ai"},
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    data = patch_resp.json()["info"]
    assert data["name"] == "New Name"
    assert data["url"] == "https://new.tunnel.unify.ai"
    assert data["os"] == "macos"


@pytest.mark.anyio
async def test_update_desktop_empty_body(client: AsyncClient):
    create_resp = await client.post(
        "/v0/desktop",
        json={
            "name": "Static",
            "url": "https://static.tunnel.unify.ai",
            "os": "ubuntu",
        },
        headers=HEADERS,
    )
    desktop_id = create_resp.json()["info"]["id"]

    patch_resp = await client.patch(
        f"/v0/desktop/{desktop_id}",
        json={},
        headers=HEADERS,
    )
    assert patch_resp.status_code == 400


@pytest.mark.anyio
async def test_update_desktop_not_found(client: AsyncClient):
    patch_resp = await client.patch(
        "/v0/desktop/999999",
        json={"name": "Ghost"},
        headers=HEADERS,
    )
    assert patch_resp.status_code == 404


@pytest.mark.anyio
async def test_delete_desktop(client: AsyncClient):
    create_resp = await client.post(
        "/v0/desktop",
        json={
            "name": "Doomed",
            "url": "https://doomed.tunnel.unify.ai",
            "os": "windows",
        },
        headers=HEADERS,
    )
    desktop_id = create_resp.json()["info"]["id"]

    del_resp = await client.delete(f"/v0/desktop/{desktop_id}", headers=HEADERS)
    assert del_resp.status_code == 200
    assert "deleted" in del_resp.json()["info"].lower()

    list_resp = await client.get("/v0/desktop", headers=HEADERS)
    assert all(d["id"] != desktop_id for d in list_resp.json()["info"])


@pytest.mark.anyio
async def test_delete_desktop_not_found(client: AsyncClient):
    del_resp = await client.delete("/v0/desktop/999999", headers=HEADERS)
    assert del_resp.status_code == 404


@pytest.mark.anyio
async def test_register_desktop_invalid_os(client: AsyncClient):
    resp = await client.post(
        "/v0/desktop",
        json={"name": "Bad OS", "url": "https://bad.tunnel.unify.ai", "os": "freebsd"},
        headers=HEADERS,
    )
    assert resp.status_code == 422


# =============================================================================
# Desktop assignment and listing
# =============================================================================


@pytest.mark.anyio
async def test_list_desktop_shows_assignment(client: AsyncClient):
    desktop_resp = await client.post(
        "/v0/desktop",
        json={
            "name": "Assigned Desktop",
            "url": "https://assigned.tunnel.unify.ai",
            "os": "macos",
        },
        headers=HEADERS,
    )
    desktop_id = desktop_resp.json()["info"]["id"]

    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Assignee", "surname": "Bot", "create_infra": False},
        headers=HEADERS,
    )
    agent_id = create_resp.json()["info"]["agent_id"]

    await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"user_desktop_id": desktop_id, "create_infra": False},
        headers=HEADERS,
    )

    list_resp = await client.get("/v0/desktop", headers=HEADERS)
    desktops = list_resp.json()["info"]
    matched = [d for d in desktops if d["id"] == desktop_id]
    assert len(matched) == 1
    assert matched[0]["assigned_to_assistant_id"] == int(agent_id)


# =============================================================================
# Delete desktop unlinks from assistant
# =============================================================================


@pytest.mark.anyio
async def test_delete_assigned_desktop_unlinks_assistant(client: AsyncClient):
    desktop_resp = await client.post(
        "/v0/desktop",
        json={
            "name": "Unlink Me",
            "url": "https://unlink.tunnel.unify.ai",
            "os": "ubuntu",
        },
        headers=HEADERS,
    )
    desktop_id = desktop_resp.json()["info"]["id"]

    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Linked", "surname": "Assistant", "create_infra": False},
        headers=HEADERS,
    )
    agent_id = create_resp.json()["info"]["agent_id"]

    patch_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"user_desktop_id": desktop_id, "create_infra": False},
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["info"]["user_desktop_id"] == desktop_id
    assert (
        patch_resp.json()["info"]["user_desktop_url"]
        == "https://unlink.tunnel.unify.ai"
    )

    del_resp = await client.delete(f"/v0/desktop/{desktop_id}", headers=HEADERS)
    assert del_resp.status_code == 200

    assistants_resp = await client.get("/v0/assistant", headers=HEADERS)
    assistant_data = [
        a for a in assistants_resp.json()["info"] if a["agent_id"] == agent_id
    ][0]
    assert assistant_data["user_desktop_id"] is None
    assert assistant_data["user_desktop_url"] is None
    assert assistant_data["user_desktop_mode"] is None


@pytest.mark.anyio
async def test_delete_unassigned_desktop_succeeds(client: AsyncClient):
    desktop_resp = await client.post(
        "/v0/desktop",
        json={
            "name": "Standalone",
            "url": "https://standalone.tunnel.unify.ai",
            "os": "macos",
        },
        headers=HEADERS,
    )
    desktop_id = desktop_resp.json()["info"]["id"]

    del_resp = await client.delete(f"/v0/desktop/{desktop_id}", headers=HEADERS)
    assert del_resp.status_code == 200

    list_resp = await client.get("/v0/desktop", headers=HEADERS)
    assert all(d["id"] != desktop_id for d in list_resp.json()["info"])
