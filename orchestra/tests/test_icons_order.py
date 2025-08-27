import os

import pytest
from httpx import AsyncClient

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {"accept": "application/json", "Authorization": f"Bearer {api_key}"}


async def _create_proj(client: AsyncClient, name: str, icon: str | None = None):
    payload = {"name": name}
    if icon:
        payload["icon"] = icon
    resp = await client.post("/v0/project", json=payload, headers=HEADERS)
    assert resp.status_code == 200


async def _create_interface(
    client: AsyncClient,
    project: str,
    name: str,
    order: int | None = None,
    icon: str | None = None,
):
    payload = {"name": name, "project": project}
    if order is not None:
        payload["order"] = order
    if icon:
        payload["icon"] = icon
    resp = await client.post("/v0/interfaces/", json=payload, headers=HEADERS)
    assert resp.status_code in (200, 201)
    return resp.json()["id"]


async def _create_tab(
    client: AsyncClient,
    interface_id: str,
    name: str,
    order: int | None = None,
    icon: str | None = None,
):
    payload = {"name": name, "interface_id": interface_id}
    if order is not None:
        payload["order"] = order
    if icon:
        payload["icon"] = icon
    resp = await client.post("/v0/tab", json=payload, headers=HEADERS)
    assert resp.status_code in (200, 201)
    return resp.json()["id"]


@pytest.mark.anyio
async def test_project_interface_tab_icons_and_order(client: AsyncClient):
    project_name = "icon-order-project"
    await _create_proj(client, project_name)

    # update project icon and order
    update = {"icon": "star", "order": 5}
    resp = await client.patch(
        f"/v0/project/{project_name}",
        json=update,
        headers=HEADERS,
    )
    assert resp.status_code == 200

    # fetch project and verify fields
    resp = await client.get(f"/v0/project/{project_name}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["icon"] == "star"

    # create two interfaces with different order
    iface1_id = await _create_interface(
        client,
        project_name,
        "if1",
        order=1,
        icon="panel",
    )
    iface2_id = await _create_interface(
        client,
        project_name,
        "if2",
        order=0,
        icon="panel2",
    )

    # update interface icon and order
    resp = await client.put(
        f"/v0/interface/{iface1_id}",
        json={"icon": "panel-updated", "order": 2},
        headers=HEADERS,
    )
    assert resp.status_code == 200

    # create tab
    tab_id = await _create_tab(client, iface1_id, "tab1", order=0, icon="graph")

    # update tab icon and order
    resp = await client.put(
        "/v0/tab",
        params={"tab_id": tab_id},
        json={"icon": "graph-updated", "order": 3},
        headers=HEADERS,
    )
    assert resp.status_code == 200

    # verify tree
    resp = await client.get("/v0/projects/tree", headers=HEADERS)
    assert resp.status_code == 200
    proj_entry = next(p for p in resp.json() if p["project"] == project_name)
    assert proj_entry["order"] == 5
    # interfaces are sorted by order; first should be if2 with order 0
    assert proj_entry["interfaces"][0]["name"] == "if2"
    tab_entry = proj_entry["interfaces"][1]["tabs"][0]
    assert tab_entry["icon"] == "graph-updated"
    assert tab_entry["order"] == 3
