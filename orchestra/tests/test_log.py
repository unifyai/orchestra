import os

import pytest
from httpx import AsyncClient

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

log_data = {
    "logs": {
        "input": "Some input data",
        "boolean_input": True,
        "numeric_input": 4.5,
    },
}


def _create_logs(client, project_name):
    return client.post(
        "/v0/log",
        json={"project": project_name, "logs": log_data["logs"]},
        headers=HEADERS,
    )


def _create_project(client, project_name):
    url = "/v0/project"
    project_data = {"name": project_name}
    return client.post(url, json=project_data, headers=HEADERS)


@pytest.mark.anyio
async def test_create_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    response = await _create_logs(client, project_name)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), int)

    # TODO: Get log and see if it matches


@pytest.mark.anyio
async def test_create_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    response = await _create_logs(client, project_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "A project with this name doesn't exists."}


@pytest.mark.anyio
async def test_delete_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_logs(client, project_name)
    log_id = log_response.json()

    # delete the log
    response = await client.delete(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Log deleted successfully!"}

    # TODO: Try to fetch the deleted log


@pytest.mark.anyio
async def test_delete_log_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log does not exist
    response = await client.delete(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found in your account.",
    }


@pytest.mark.anyio
async def test_delete_log_entry(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_logs(client, project_name)
    log_id = log_response.json()

    # delete an entry in the log
    response = await client.delete(f"/v0/log/{log_id}/entry/input", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Log entry deleted successfully!"}

    # TODO: Fetch before and after to check entries


@pytest.mark.anyio
async def test_delete_log_entry_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log entry does not exist
    response = await client.delete(f"/v0/log/{log_id}/entry/input", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found in your account.",
    }

    # TODO: There are a couple more exceptions not being tested I think


@pytest.mark.anyio
async def test_get_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_logs(client, project_name)
    log_id = log_response.json()

    # fetch the log
    response = await client.get(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert "entries" in response.json()  # Log entries are returned
    assert isinstance(response.json()["entries"]["boolean_input"], bool)
    assert isinstance(response.json()["entries"]["numeric_input"], float)


@pytest.mark.anyio
async def test_get_log_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log does not exist
    response = await client.get(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found in your account.",
    }


@pytest.mark.anyio
async def test_get_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs(client, project_name)

    # fetch logs for the project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    # TODO: Test filter_expr

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)  # List of logs is returned
    assert isinstance(response.json()[0]["entries"]["boolean_input"], bool)
    assert isinstance(response.json()[0]["entries"]["numeric_input"], float)


@pytest.mark.anyio
async def test_get_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found in your account.",
    }


@pytest.mark.anyio
async def test_get_logs_groups(client: AsyncClient):
    # TODO: Test this further
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs(client, project_name)

    # fetch log groups for a given key
    response = await client.get(
        f"/v0/logs/groups?project={project_name}&key=input",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)  # Ensure it's a list of grouped entries


@pytest.mark.anyio
async def test_get_logs_groups_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await client.get(
        f"/v0/logs/groups?project={project_name}&key=input",
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found in your account.",
    }
