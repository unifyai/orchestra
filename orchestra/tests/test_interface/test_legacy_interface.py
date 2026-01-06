import os

import pytest
from httpx import AsyncClient

# Common headers and data
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def _create_project(client: AsyncClient, project):
    return client.post("/v0/project", json={"name": project}, headers=HEADERS)


def _create_context(client: AsyncClient, project, name, description):
    return client.post(
        f"/v0/project/{project}/contexts",
        json={"name": name, "description": description},
        headers=HEADERS,
    )


def _create_interface(
    client: AsyncClient,
    name,
    project,
    items,
    new_counter,
    context=None,
    temporary=False,
):
    return client.post(
        "/v0/interface",
        headers=HEADERS,
        json={
            "name": name,
            "project_name": project,
            "items": items,
            "new_counter": new_counter,
            "context": context,
            "temporary": temporary,
        },
    )


@pytest.mark.skip(reason="Legacy interface API has been deprecated and removed")
@pytest.mark.anyio
async def test_create_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    context = "my_context"
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
    _ = await _create_project(client, project)
    _ = await _create_context(client, project, context, "")
    response = await _create_interface(
        client,
        name,
        project,
        items,
        new_counter,
        context,
    )
    assert response.status_code == 200
    assert "id" in response.json()
    assert response.json()["id"] is not None


@pytest.mark.skip(reason="Legacy interface API has been deprecated and removed")
@pytest.mark.anyio
async def test_update_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    context = "my_context"
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
    _ = await _create_project(client, project)
    _ = await _create_context(client, project, context, "")
    _ = await _create_interface(
        client,
        name,
        project,
        items[:1],
        new_counter - 1,
        context,
    )
    response = await client.put(
        "/v0/interface",
        headers=HEADERS,
        json={
            "name": name,
            "project_name": project,
            "items": items,
            "new_counter": new_counter,
            "new_name": "my_new_interface",
            "context": context,
        },
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Interface updated successfully!"


@pytest.mark.skip(reason="Legacy interface API has been deprecated and removed")
@pytest.mark.anyio
async def test_get_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    context = "my_context"
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
    _ = await _create_project(client, project)
    _ = await _create_context(client, project, context, "")
    _ = await _create_interface(client, name, project, items, new_counter, context)
    response = await client.get(
        f"/v0/interface?name={name}&project_name={project}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.skip(reason="Legacy interface API has been deprecated and removed")
@pytest.mark.anyio
async def test_delete_interface(client: AsyncClient):
    name = "my_interface"
    project = "my_project"
    context = "my_context"
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
    _ = await _create_project(client, project)
    _ = await _create_context(client, project, context, "")
    _ = await _create_interface(client, name, project, items, new_counter, context)
    response = await client.delete(
        f"/v0/interface?name={name}&project_name={project}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Interface deleted successfully!"


@pytest.mark.skip(reason="Legacy interface API has been deprecated and removed")
@pytest.mark.anyio
async def test_delete_project_deletes_interfaces_and_temp_interfaces(
    client: AsyncClient,
):
    project_name = "test-project"
    _ = await _create_project(client, project_name)
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
    _ = await _create_interface(client, "test-interface", project_name, items, 1)
    _ = await _create_interface(
        client,
        "test-temp-interface",
        project_name,
        items,
        1,
        context="test-context",
        temporary=True,
    )
    response = await client.delete(f"/v0/project/{project_name}", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project deleted successfully"

    # Verify interfaces and temp interfaces are deleted
    response = await client.get(
        f"/v0/interface?name=test-interface&project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()  # should not be found

    response = await client.get(
        f"/v0/interface?name=test-temp-interface&project_name={project_name}&temporary=true",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()  # should not be found
