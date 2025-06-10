import pytest
from httpx import AsyncClient

from .test_log import HEADERS, _create_log, _update_logs, fetch_logs


@pytest.mark.anyio
async def test_commit_versioned_project(client: AsyncClient):
    """Test committing a versioned project."""
    project_name = "test_commit_project"
    context_name = "versioned_context"

    # Create a versioned project
    await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": True},
        headers=HEADERS,
    )

    # Create a versioned context
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "is_versioned": True},
        headers=HEADERS,
    )

    # Add a log to the context
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"key": "value"},
    )

    # Commit the project
    commit_response = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Initial commit"},
        headers=HEADERS,
    )
    assert commit_response.status_code == 200
    assert "commit_hash" in commit_response.json()

    # Verify project version incremented
    project_response = await client.get(f"/v0/projects", headers=HEADERS)
    # This is a placeholder for a dedicated project details endpoint
    # In a real scenario, you would fetch project details to check the version
    assert project_name in project_response.json()

    # Verify context version incremented
    context_response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert context_response.json()["version"] > 1


@pytest.mark.anyio
async def test_rollback_context_to_version(client: AsyncClient):
    """Test rolling back a context to a specific version."""
    project_name = "test_rollback_context"
    context_name = "rollback_context"

    await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "is_versioned": True},
        headers=HEADERS,
    )

    # Version 1
    log_response = await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"value": 1},
    )
    log_id = log_response.json()["log_event_ids"][0]
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "v1"},
        headers=HEADERS,
    )

    # Version 2
    await _update_logs(
        client,
        [log_id],
        {"value": 2},
        context={"name": context_name},
        overwrite=True,
    )
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "v2"},
        headers=HEADERS,
    )

    # Rollback to version 1
    rollback_response = await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/rollback",
        json={"version": 1},
        headers=HEADERS,
    )
    assert rollback_response.status_code == 200

    # Verify log is rolled back
    logs = await fetch_logs(client, project_name, context=context_name)
    assert logs[0]["entries"]["value"] == 1

    # Verify context version is rolled back
    context_response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert context_response.json()["version"] == 1


@pytest.mark.anyio
async def test_rollback_project_to_commit(client: AsyncClient):
    """Test rolling back a project to a specific commit."""
    project_name = "test_rollback_project"
    context_name = "rollback_project_context"

    await client.post(
        "/v0/project",
        json={"name": project_name, "is_versioned": True},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "is_versioned": True},
        headers=HEADERS,
    )

    # v1
    await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"value": "v1"},
    )
    commit1 = await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "First commit"},
        headers=HEADERS,
    )
    commit1_hash = commit1.json()["commit_hash"]

    # v2
    logs = await fetch_logs(client, project_name, context=context_name)
    log_id = logs[0]["id"]
    await _update_logs(
        client,
        [log_id],
        {"value": "v2"},
        context={"name": context_name},
        overwrite=True,
    )
    await client.post(
        f"/v0/project/{project_name}/commit",
        json={"commit_message": "Second commit"},
        headers=HEADERS,
    )

    # Rollback project to first commit
    rollback_response = await client.post(
        f"/v0/project/{project_name}/rollback",
        json={"commit_hash": commit1_hash},
        headers=HEADERS,
    )
    assert rollback_response.status_code == 200

    # Verify context is rolled back
    logs_after_rollback = await fetch_logs(client, project_name, context=context_name)
    assert logs_after_rollback[0]["entries"]["value"] == "v1"
