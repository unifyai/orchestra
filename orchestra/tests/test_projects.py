import os

import pytest
from httpx import AsyncClient

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


if __name__ == "__main__":
    pass
