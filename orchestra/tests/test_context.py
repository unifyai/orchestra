import os
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient, Request

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


@pytest.mark.anyio
async def test_create_context(client: AsyncClient):
    project_name = "test-project"

    # Create project first
    response = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context
    context_data = {
        "name": "training",
        "description": "Training context for agent evaluation",
    }
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json=context_data,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Context created successfully" in response.json()["info"]


@pytest.mark.anyio
async def test_create_existing_context(client: AsyncClient):
    project_name = "test-project"
    context_name = "existing-context"

    # Create project first
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create context
    context_data = {
        "name": context_name,
        "description": "Test context",
    }
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json=context_data,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to create same context again
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json=context_data,
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert (
        "A context with this name already exists in the project"
        in response.json()["detail"]
    )


@pytest.mark.anyio
async def test_delete_context(client: AsyncClient):
    project_name = "test-project"
    context_name = "test-context"

    # Create project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context"},
        headers=HEADERS,
    )

    # Delete context
    request = Request(
        "DELETE",
        str(client.base_url) + f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    response = await client.send(request)

    assert response.status_code == 200
    assert "Context deleted successfully" in response.json()["info"]


@pytest.mark.anyio
async def test_add_log_to_context(client: AsyncClient):
    project_name = "test-project"
    context_name = "test-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context"},
        headers=HEADERS,
    )

    # Create log with context
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
        headers=HEADERS,
    )
    log_ids = response.json()
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": context_name, "log_ids": log_ids},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Logs added to context successfully!" in response.json()["info"]


@pytest.mark.anyio
async def test_get_logs_by_context(client: AsyncClient):
    project_name = "test-project"
    context_name = "test-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context"},
        headers=HEADERS,
    )

    # Create log without context
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
        headers=HEADERS,
    )

    # create log with context
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "metric": 1.5,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "context": {
                "name": "different-context",
                "description": "Different context",
            },
        },
        headers=HEADERS,
    )

    # Get logs by context (only one log should be returned)
    response = await client.get(
        f"/v0/logs?project={project_name}&context=different-context",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["metric"] == 1.5


@pytest.mark.anyio
async def test_add_log_to_multiple_contexts(client: AsyncClient):
    project_name = "test-project"
    contexts = ["training", "evaluation"]

    # Setup project and contexts
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    for context in contexts:
        await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": context, "description": f"{context} context"},
            headers=HEADERS,
        )

    # Add log to multiple contexts
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
        headers=HEADERS,
    )
    log_ids = response.json()
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": contexts[0], "log_ids": log_ids},
        headers=HEADERS,
    )
    assert response.status_code == 200
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": contexts[1], "log_ids": log_ids},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify that log appears in both contexts
    for context in contexts:
        response = await client.get(
            f"/v0/logs?project={project_name}&context={context}",
            headers=HEADERS,
        )
        assert response.status_code == 200
        logs = response.json()["logs"]
        assert len(logs) == 1
        assert logs[0]["entries"]["metric"] == 0.95


@pytest.mark.anyio
async def test_context_artifacts(client: AsyncClient):
    project_name = "test-project"
    context_name = "test-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context"},
        headers=HEADERS,
    )

    # Add artifact to context
    artifact_data = {
        "artifacts": {
            "model.pkl": {
                "type": "model",
                "metadata": {"framework": "pytorch", "version": "1.0"},
            },
        },
    }
    response = await client.post(
        f"/v0/project/{project_name}/contexts/{context_name}/artifacts",
        json=artifact_data,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Get context artifacts
    response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}/artifacts",
        headers=HEADERS,
    )
    assert response.status_code == 200
    artifacts = response.json()
    assert len(artifacts) == 1
    assert artifacts["model.pkl"] == {
        "type": "model",
        "metadata": {"framework": "pytorch", "version": "1.0"},
    }
