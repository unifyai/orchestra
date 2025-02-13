import os

import pytest
from httpx import AsyncClient

from .test_interface import _create_context, _create_interface, _create_project
from .test_log import _create_log

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


@pytest.mark.anyio
async def test_create_project(client: AsyncClient):
    url = "/v0/project"
    project_data = {"name": "test-project"}
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Project created successfully!"


@pytest.mark.anyio
async def test_create_existing_project(client: AsyncClient):
    url = "/v0/project"
    project_data = {
        "name": "existing-project",
    }

    # Create the project first
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # Try to create the same project again
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 400
    assert (
        response.json()["detail"] == "A logging project with this name already exists."
    )


@pytest.mark.anyio
async def test_delete_project(client: AsyncClient):
    url = "/v0/project/test-project"

    # Create a project first to delete it
    create_response = await client.post(
        "/v0/project",
        json={"name": "test-project"},
        headers=HEADERS,
    )
    assert create_response.status_code == 200

    # Now delete the project
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project deleted successfully"


@pytest.mark.anyio
async def test_delete_nonexistent_project(client: AsyncClient):
    url = "/v0/project/nonexistent-project"
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Project nonexistent-project not found."


@pytest.mark.anyio
async def test_rename_project(client: AsyncClient):
    create_url = "/v0/project"
    rename_url = "/v0/project/test-project"
    project_data = {"name": "test-project"}

    # Create a project to rename
    respose = await client.post(create_url, json=project_data, headers=HEADERS)

    # Rename the project
    rename_data = {"name": "renamed-project"}
    response = await client.patch(rename_url, json=rename_data, headers=HEADERS)
    print(response)
    assert response.status_code == 200
    assert response.json()["info"] == "Project renamed successfully!"


@pytest.mark.anyio
async def test_rename_nonexistent_project(client: AsyncClient):
    url = "/v0/project/nonexistent-project"
    project_data = {"name": "renamed-project"}
    response = await client.patch(url, json=project_data, headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Project nonexistent-project not found."


@pytest.mark.anyio
async def test_list_projects(client: AsyncClient):
    # Add two projects first
    await client.post("/v0/project", json={"name": "project_a"}, headers=HEADERS)
    await client.post("/v0/project", json={"name": "project_b"}, headers=HEADERS)

    # List the projects
    url = "/v0/projects"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200
    projects = response.json()
    assert "project_a" in projects
    assert "project_b" in projects


@pytest.mark.anyio
async def test_delete_project_logs(client: AsyncClient):
    # Create a test project with context, interface and logs
    project = "test_project"
    context = "test_context"
    interface_name = "test_interface"
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

    # Create project and its components
    await _create_project(client, project)
    await _create_context(client, project, context, "test description")
    await _create_interface(
        client,
        interface_name,
        project,
        items,
        new_counter,
    )

    # Create some logs in the project
    for i in range(3):
        _ = await _create_log(
            client,
            project,
            context={"name": context},
            entries={"test_key": f"test_value_{i}", "another_key": 123},
        )

    # Call delete_project_logs endpoint
    delete_response = await client.delete(
        f"/v0/project/{project}/logs",
        headers=HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify logs are deleted
    logs_response = await client.get(
        f"/v0/logs",
        headers=HEADERS,
        params={"project": project},
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 0  # No logs should remain

    # Verify context still exists
    context_response = await client.get(
        f"/v0/project/{project}/contexts",
        headers=HEADERS,
    )
    assert context_response.status_code == 200
    contexts = context_response.json()
    assert len(contexts) > 0
    assert contexts[0]["name"] == context

    # Verify interface still exists
    interface_response = await client.get(
        f"/v0/interface?name={interface_name}&project={project}",
        headers=HEADERS,
    )
    assert interface_response.status_code == 200
    interfaces = interface_response.json()
    assert len(interfaces) > 0
    assert interfaces[0]["name"] == interface_name


@pytest.mark.anyio
async def test_delete_project_contexts(client: AsyncClient):
    # Create a test project with multiple contexts, interface and logs
    project = "test_project_contexts"
    context1 = "test_context_1"
    context2 = "test_context_2"
    interface_name = "test_interface"
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

    # Create project and its components
    await _create_project(client, project)
    await _create_context(client, project, context1, "test description 1")
    await _create_context(client, project, context2, "test description 2")
    await _create_interface(
        client,
        interface_name,
        project,
        items,
        new_counter,
    )

    # Create logs for each context
    await _create_log(
        client,
        project,
        context={"name": context1},
        entries={"test_key": "value1"},
    )
    await _create_log(
        client,
        project,
        context={"name": context2},
        entries={"test_key": "value2"},
    )

    # Call delete_project_contexts endpoint
    delete_response = await client.delete(
        f"/v0/project/{project}/contexts",
        headers=HEADERS,
    )
    assert delete_response.status_code == 200
    assert (
        delete_response.json()["info"]
        == "Project contexts and logs deleted successfully!"
    )

    # Verify logs are deleted
    logs_response = await client.get(
        f"/v0/logs",
        headers=HEADERS,
        params={"project": project},
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 0  # No logs should remain

    # Verify contexts are deleted
    contexts_response = await client.get(
        f"/v0/project/{project}/contexts",
        headers=HEADERS,
    )
    assert contexts_response.status_code == 200
    contexts = contexts_response.json()
    assert len(contexts) == 0  # No contexts should remain

    # Verify interface still exists
    interface_response = await client.get(
        f"/v0/interface?name={interface_name}&project={project}",
        headers=HEADERS,
    )
    assert interface_response.status_code == 200
    interfaces = interface_response.json()
    assert len(interfaces) > 0
    assert interfaces[0]["name"] == interface_name


if __name__ == "__main__":
    pass
