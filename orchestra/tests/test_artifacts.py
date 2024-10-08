import os

import pytest
from httpx import AsyncClient

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

artifact_data = {
    "artifacts": {
        "artifact_1": "value_1",
        "artifact_2": "value_2",
    },
}


def _create_artifacts(client, project_name):
    return client.post(
        f"/v0/log/project/{project_name}/artifacts",
        json=artifact_data,
        headers=HEADERS,
    )


def _create_project(client, project_name):
    url = "/v0/log/project"
    project_data = {"name": project_name}
    return client.post(url, json=project_data, headers=HEADERS)


@pytest.mark.anyio
async def test_create_artifacts(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    response = await _create_artifacts(client, project_name)

    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Artifact(s) created successfully!"}


@pytest.mark.anyio
async def test_create_artifacts_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await _create_artifacts(client, project_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "A project with this name doesn't exists."}


@pytest.mark.anyio
async def test_delete_artifact(client: AsyncClient):
    project_name = "eval-project"
    artifact_key = "artifact_1"
    _ = await _create_project(client, project_name)
    _ = await _create_artifacts(client, project_name)

    # delete artifacts
    response = await client.delete(
        f"/v0/log/project/{project_name}/artifacts/{artifact_key}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Artifact deleted successfully!"}


@pytest.mark.anyio
async def test_delete_artifact_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"
    artifact_key = "artifact_1"

    # This should return 404 as the project does not exist
    response = await client.delete(
        f"/v0/log/project/{project_name}/artifacts/{artifact_key}",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found in your account.",
    }


@pytest.mark.anyio
async def test_delete_artifact_not_found(client: AsyncClient):
    project_name = "eval-project"
    artifact_key = "non_existent_artifact"
    _ = await _create_project(client, project_name)

    # This should return 404 as the artifact does not exist
    response = await client.delete(
        f"/v0/log/project/{project_name}/artifacts/{artifact_key}",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Artifact {artifact_key} not found in this project.",
    }


@pytest.mark.anyio
async def test_list_artifacts(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_artifacts(client, project_name)

    # This should return the list of artifacts
    response = await client.get(
        f"/v0/log/project/{project_name}/artifacts",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)  # Ensure the response is a dictionary


@pytest.mark.anyio
async def test_list_artifacts_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await client.get(
        f"/v0/log/project/{project_name}/artifacts",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found in your account.",
    }
