import os

import pytest
from httpx import AsyncClient

# Common headers and data
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


async def _create_project(client: AsyncClient, project):
    return client.post("/v0/project", json={"name": project}, headers=HEADERS)


async def _create_interface(client: AsyncClient, name, project, items, new_counter):
    return client.post(
        "/v0/interface",
        headers=HEADERS,
        json={
            "name": name,
            "project": project,
            "items": items,
            "new_counter": new_counter,
        },
    )


@pytest.mark.anyio
async def test_create_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    items = [
        {
            "i": "n0",
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "tab": None,
            "moved": False,
            "static": False,
        },
    ]
    new_counter = 1
    await _create_project(client, project)
    response = await _create_interface(client, name, project, items, new_counter)
    assert response.status_code == 200
    assert response.json()["info"] == "Interface created successfully!"


@pytest.mark.anyio
async def test_update_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    items = [
        {
            "i": "n0",
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "tab": None,
            "moved": False,
            "static": False,
        },
        {
            "i": "n1",
            "x": 0,
            "y": 1,
            "w": 2,
            "h": 3,
            "tab": "Plot_1",
            "moved": False,
            "static": False,
        },
    ]
    new_counter = 2
    await _create_project(client, project)
    await _create_interface(client, name, project, items[:1], new_counter - 1)
    response = await client.put(
        "/v0/interface",
        headers=HEADERS,
        json={
            "name": name,
            "project": project,
            "items": items,
            "new_counter": new_counter,
            "new_name": "my_new_interface",
        },
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Interface updated successfully!"


@pytest.mark.anyio
async def test_get_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    items = [
        {
            "i": "n0",
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "tab": None,
            "moved": False,
            "static": False,
        },
    ]
    new_counter = 1
    await _create_project(client, project)
    await _create_interface(client, name, project, items, new_counter)
    response = await client.get("/v0/interface", headers=HEADERS)
    assert response.status_code == 200
    assert isinstance(response.json(), dict)


@pytest.mark.anyio
async def test_delete_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    items = [
        {
            "i": "n0",
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "tab": None,
            "moved": False,
            "static": False,
        },
    ]
    new_counter = 1
    await _create_interface(client, name, project, items, new_counter)
    response = await client.delete(
        "/v0/interface",
        headers=HEADERS,
        json={
            "name": name,
            "project": project,
            "items": items,
            "new_counter": new_counter,
        },
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Interface deleted successfully!"
