import json
import os
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient, Request

from .test_log import HEADERS, _create_log, _create_project, _update_logs, fetch_logs

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
async def test_get_contexts(client: AsyncClient):
    project_name = "test-project"
    context_name = "test-context"

    # Create project and context
    response = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200

    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context"},
        headers=HEADERS,
    )

    # Get contexts
    response = await client.get(
        f"/v0/project/{project_name}/contexts",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()[0]["name"] == context_name


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
    log_ids = response.json()["log_event_ids"]
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": context_name, "log_ids": log_ids},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Logs added to context successfully!" in response.json()["info"]


@pytest.mark.anyio
async def test_implicit_context_creation(client: AsyncClient):
    """Test that a context is implicitly created when adding logs to a non-existent context"""
    project_name = "test-implicit-context"
    context_name = "implicit-context"

    # Create project first
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create log without context
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"param1": "test"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_ids = response.json()["log_event_ids"]

    # Add logs to a context that doesn't exist yet - should create it implicitly
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": context_name, "log_ids": log_ids},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Logs added to context successfully!" in response.json()["info"]

    # Verify the context was created
    response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["name"] == context_name


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
async def test_context_as_string(client: AsyncClient):
    """Test that context can be provided as a string instead of an object"""
    project_name = "test-string-context"
    context_name = "string-context"

    # Create project first
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create context using string name
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name},  # No description provided
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Context created successfully" in response.json()["info"]

    # Create log with context as string
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"param1": "test"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "context": context_name,  # Provide context as string
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Get logs by context string
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["metric"] == 0.95


async def test_get_logs_no_context(client: AsyncClient):
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

    # Create log within context
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "context": {
                "name": context_name,
                "description": "Test context",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Get logs without context (should return 0 logs)
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 0


async def test_get_fields_no_context(client: AsyncClient):
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

    # Create log within context
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "context": {
                "name": context_name,
                "description": "Test context",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Get fields with context
    response = await client.get(
        f"/v0/logs/fields?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert len(fields) == 3
    assert list(fields.keys()) == ["a/b/param1", "metric", "timestamp"]

    # Get fields without context (should return 0 fields)
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert len(fields) == 0


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
    log_ids = response.json()["log_event_ids"]
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


@pytest.mark.anyio
async def test_versioned_context_behavior(client: AsyncClient):
    """Test that versioned contexts track changes to their logs correctly"""

    # Create a project
    project_name = "test_versioned_context"
    await _create_project(client, project_name)

    # Create a versioned context
    context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "versioned_context",
            "description": "A versioned context",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert context_response.status_code == 200

    # Create logs in the versioned context - should be automatically versioned
    log_response = await _create_log(
        client,
        project_name,
        entries={"field1": "value1"},
        context={"name": "versioned_context"},
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Verify context version incremented
    context_get = await client.get(
        f"/v0/project/{project_name}/contexts/versioned_context",
        headers=HEADERS,
    )
    assert context_get.json()["version"] == 2

    # Update log - should increment both log and context versions
    update_response = await _update_logs(
        client,
        [log_id],
        {"field1": "value2"},
        context={"name": "versioned_context"},
        overwrite=True,
    )
    assert update_response.status_code == 200

    # Verify context version incremented again
    context_get = await client.get(
        f"/v0/project/{project_name}/contexts/versioned_context",
        headers=HEADERS,
    )
    assert context_get.json()["version"] == 3

    # Get log versions
    log_data = await fetch_logs(
        client,
        project_name,
        return_versions=True,
        context="versioned_context",
    )
    assert len(log_data) == 1
    log = log_data[0]
    assert len(log["versions"]["field1"]) == 2
    assert "value1" in log["versions"]["field1"].values()
    assert "value2" in log["versions"]["field1"].values()


@pytest.mark.anyio
async def test_update_logs_with_string_context(client: AsyncClient):
    """Test that update_logs endpoint accepts context as a string"""
    project_name = "test-update-string-context"
    context_name = "update-string-context"

    # Create project and context
    await _create_project(client, project_name)
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context"},
        headers=HEADERS,
    )

    # Create log with context
    log_response = await _create_log(
        client,
        project_name,
        entries={"field1": "value1"},
        context=context_name,  # Provide context as string
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Update log with context as string
    update_response = await _update_logs(
        client,
        [log_id],
        {"field1": "value2"},
        context=context_name,  # Provide context as string
        overwrite=True,
    )
    assert update_response.status_code == 200

    # Verify log was updated
    logs = await fetch_logs(
        client,
        project_name,
        context=context_name,
    )
    assert len(logs) == 1
    assert logs[0]["entries"]["field1"] == "value2"


@pytest.mark.anyio
async def test_unversioned_context_behavior(client: AsyncClient):
    """Test that unversioned contexts don't track versions"""

    # Create a project
    project_name = "test_unversioned_context"
    await _create_project(client, project_name)

    # Create an unversioned context
    context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "unversioned_context",
            "description": "An unversioned context",
        },
        headers=HEADERS,
    )
    assert context_response.status_code == 200

    # Create logs in the unversioned context
    log_response = await _create_log(
        client,
        project_name,
        entries={"field1": "value1"},
        context={"name": "unversioned_context"},
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Verify context version unchanged
    context_get = await client.get(
        f"/v0/project/{project_name}/contexts/unversioned_context",
        headers=HEADERS,
    )
    assert context_get.json()["version"] == 1

    # Update log - should not affect versions
    update_response = await _update_logs(
        client,
        [log_id],
        {"field1": "value2"},
        context={"name": "unversioned_context"},
        overwrite=True,
    )
    assert update_response.status_code == 200

    # Verify context version still unchanged
    context_get = await client.get(
        f"/v0/project/{project_name}/contexts/unversioned_context",
        headers=HEADERS,
    )
    assert context_get.json()["version"] == 1

    # Verify return_versions=True raises exception for unversioned context
    response = await client.get(
        f"/v0/logs?project={project_name}&context=unversioned_context&return_versions=True",
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Cannot return versions for unversioned context" in str(
        response.json()["detail"],
    )


@pytest.mark.anyio
async def test_versioning_constraints(client: AsyncClient):
    """Test that versioning constraints are enforced correctly"""

    # Create a project
    project_name = "test_versioning_constraints"
    await _create_project(client, project_name)

    # Create a versioned context
    versioned_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "versioned_context",
            "description": "A versioned context",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert versioned_context_response.status_code == 200

    # Create an unversioned context
    unversioned_context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "unversioned_context",
            "description": "An unversioned context",
            "is_versioned": False,
        },
        headers=HEADERS,
    )
    assert unversioned_context_response.status_code == 200

    # Create a log in versioned context - should be automatically versioned and mutable
    log_response = await _create_log(
        client,
        project_name,
        entries={
            "field1": "value1",
            "explicit_types": {
                "field1": {
                    "type": "str",
                    "mutable": False,  # This should be ignored since context is versioned
                },
            },
        },
        context={"name": "versioned_context"},
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Verify the field is mutable despite explicit setting
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        params={"context": "versioned_context"},
        headers=HEADERS,
    )
    fields_data = fields_response.json()
    assert fields_data["field1"]["mutable"] == True

    # Update should succeed since field is mutable in versioned context
    update_response = await _update_logs(
        client,
        [log_id],
        {"field1": "value2"},
        context={"name": "versioned_context"},
        overwrite=True,
    )
    assert update_response.status_code == 200

    # Create a log in unversioned context
    log_response = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "field2": "value1",
            "explicit_types": {
                "field2": {
                    "type": "str",
                    "mutable": False,
                },
            },
        },
        context={"name": "unversioned_context"},
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Verify the field is immutable as specified
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        params={"context": "unversioned_context"},
        headers=HEADERS,
    )
    fields_data = fields_response.json()
    assert fields_data["field2"]["mutable"] == False

    # Update should fail since field is immutable in unversioned context
    update_response = await _update_logs(
        client,
        [log_id],
        {"field2": "value2"},
        context={"name": "unversioned_context"},
        overwrite=True,
    )
    assert update_response.status_code == 400
    assert "immutable" in update_response.json()["detail"]

    # Try to get versions from unversioned context - should fail
    response = await client.get(
        f"/v0/logs?project={project_name}&context=unversioned_context&return_versions=True",
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Cannot return versions for unversioned context" in str(
        response.json()["detail"],
    )

    # Get versions from versioned context - should succeed
    log_data = await fetch_logs(
        client,
        project_name,
        return_versions=True,
        context="versioned_context",
    )
    assert len(log_data) == 1
    log = log_data[0]
    assert len(log["versions"]["field1"]) == 2
    assert "value1" in log["versions"]["field1"].values()
    assert "value2" in log["versions"]["field1"].values()


@pytest.mark.anyio
async def test_version_retrieval(client: AsyncClient):
    """Test retrieving specific versions of logs"""

    # Create a project
    project_name = "test_version_retrieval"
    await _create_project(client, project_name)

    # Create a versioned context
    context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "versioned_context",
            "description": "A versioned context",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert context_response.status_code == 200

    # Create a log with multiple versions
    log_response = await _create_log(
        client,
        project_name,
        entries={"field1": "value1"},
        context={"name": "versioned_context"},
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Update the log multiple times
    values = ["value2", "value3", "value4"]
    for value in values:
        update_response = await _update_logs(
            client,
            [log_id],
            {"field1": value},
            context={"name": "versioned_context"},
            overwrite=True,
        )
        assert update_response.status_code == 200

    # Get all versions
    log_data = await fetch_logs(
        client,
        project_name,
        return_versions=True,
        context="versioned_context",
    )
    assert len(log_data) == 1
    log = log_data[0]
    assert len(log["versions"]["field1"]) == 4  # Original + 3 updates

    # Verify all versions are present
    version_values = set(log["versions"]["field1"].values())
    assert "value1" in version_values
    for value in values:
        assert value in version_values


@pytest.mark.anyio
async def test_versioned_ids_only(client: AsyncClient):
    """Test that return_ids_only works correctly with versioning"""

    # Create a project
    project_name = "test_versioned_ids"
    await _create_project(client, project_name)

    # Create a versioned context
    context_response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "versioned_context",
            "description": "A versioned context",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert context_response.status_code == 200

    # Create logs in the versioned context - should be automatically versioned
    log_response = await _create_log(
        client,
        project_name,
        entries={"field1": "value1"},
        context={"name": "versioned_context"},
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Update log to create a new version
    update_response = await _update_logs(
        client,
        [log_id],
        {"field1": "value2"},
        context={"name": "versioned_context"},
        overwrite=True,
    )
    assert update_response.status_code == 200

    # Test return_ids_only without versions
    response = await client.get(
        f"/v0/logs?project={project_name}&context=versioned_context&return_ids_only=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == log_id

    # Test return_ids_only with versions
    response = await client.get(
        f"/v0/logs?project={project_name}&context=versioned_context&return_ids_only=true&return_versions=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert isinstance(result, list)
    assert len(result) == 2  # Should have two versions
    assert all("id" in item for item in result)
    assert all("version" in item for item in result)
    assert all(
        item["id"] == log_id for item in result
    )  # All entries should have same log_id
    assert (
        len({item["version"] for item in result}) == 2
    )  # Should have two distinct versions

    # Test from_ids with version information
    from_ids = [{"id": log_id, "version": result[0]["version"]}]
    response = await client.get(
        f"/v0/logs?project={project_name}&context=versioned_context&return_versions=true",
        params={"from_ids": json.dumps(from_ids)},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["field1"] == "value1"

    # Test exclude_ids with version information
    exclude_ids = [{"id": log_id, "version": 1}]
    response = await client.get(
        f"/v0/logs?project={project_name}&context=versioned_context&return_versions=true",
        params={"exclude_ids": json.dumps(exclude_ids)},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["field1"] == "value2"

    # Test invalid format for from_ids with return_versions=True
    response = await client.get(
        f"/v0/logs?project={project_name}&context=versioned_context&return_versions=true",
        params={
            "from_ids": "1&2&3",
        },  # Old format not allowed with return_versions=True
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Invalid from_ids format for versioned logs" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_logs_with_multiple_contexts(client: AsyncClient):
    """Test that update_logs endpoint accepts a list of contexts"""
    project_name = "test-multiple-contexts"
    contexts = ["context1", "context2", "context3"]

    # Create project and contexts
    await _create_project(client, project_name)
    for context in contexts:
        await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": context, "description": f"Test {context}"},
            headers=HEADERS,
        )

    # Create log with first context
    log_response = await _create_log(
        client,
        project_name,
        entries={"field1": "value1"},
        context=contexts[0],
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Add log to other contexts
    for context in contexts[1:]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": context, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200

    # Update log with multiple contexts as a list
    update_response = await _update_logs(
        client,
        [log_id],
        {"field1": "updated-value"},
        context=contexts,  # Provide contexts as a list
        overwrite=True,
    )
    assert update_response.status_code == 200

    # Verify log was updated in all contexts
    for context in contexts:
        logs = await fetch_logs(
            client,
            project_name,
            context=context,
        )
        assert len(logs) == 1
        assert logs[0]["entries"]["field1"] == "updated-value"


@pytest.mark.anyio
async def test_implicit_field_creation(client: AsyncClient):
    """Test that field types are created implicitly when adding logs to a context"""
    project_name = "test-implicit-fields"
    context_name = "implicit-fields-context"

    # Create project
    await _create_project(client, project_name)

    # Create a log with several fields
    log_response = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "metric1": 0.95,
            "metric2": 1.5,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert log_response.status_code == 200
    log_id = log_response.json()["log_event_ids"][0]

    # Create a context and add the log to it
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context for implicit fields"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Add log to the context
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": context_name, "log_ids": [log_id]},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Check that field types were created for the context
    response = await client.get(
        f"/v0/logs/fields?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()
    assert len(fields) == 3
    assert "metric1" in fields
    assert "metric2" in fields
    assert "timestamp" in fields

    # Create a new log with some overlapping and some new fields
    log_response = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "metric1": 0.85,  # Existing field
            "metric3": 2.5,  # New field
            "text": "test",  # New field
        },
    )
    assert log_response.status_code == 200
    new_log_id = log_response.json()["log_event_ids"][0]

    # Add the new log to the context
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": context_name, "log_ids": [new_log_id]},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Check that only new field types were created
    response = await client.get(
        f"/v0/logs/fields?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()
    assert len(fields) == 5  # Should now have 5 fields total
    assert "metric1" in fields  # Existing field
    assert "metric2" in fields  # Existing field
    assert "timestamp" in fields  # Existing field
    assert "metric3" in fields  # New field
    assert "text" in fields  # New field

    # Verify the types are correct
    assert fields["metric1"]["data_type"] == "float"
    assert fields["metric3"]["data_type"] == "float"
    assert fields["text"]["data_type"] == "str"


@pytest.mark.anyio
async def test_context_prefix_filtering(client: AsyncClient):
    """Test that contexts can be filtered by prefix"""
    project_name = "test-prefix-filtering"

    # Create project
    await _create_project(client, project_name)

    # Create contexts with different prefixes
    prefixes = ["Datasets/", "Experiments/", "Models/"]
    contexts = []

    for prefix in prefixes:
        # Create multiple contexts for each prefix
        for i in range(3):
            context_name = f"{prefix}context-{i}"
            contexts.append(context_name)
            response = await client.post(
                f"/v0/project/{project_name}/contexts",
                json={
                    "name": context_name,
                    "description": f"Test context {context_name}",
                },
                headers=HEADERS,
            )
            assert response.status_code == 200

    # Create some contexts without prefixes
    for i in range(2):
        context_name = f"no-prefix-{i}"
        contexts.append(context_name)
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": context_name, "description": f"Test context {context_name}"},
            headers=HEADERS,
        )
        assert response.status_code == 200

    # Get all contexts (no prefix filter)
    response = await client.get(
        f"/v0/project/{project_name}/contexts",
        headers=HEADERS,
    )
    assert response.status_code == 200
    all_contexts = response.json()
    assert len(all_contexts) == len(contexts)

    # Test filtering by each prefix
    for prefix in prefixes:
        response = await client.get(
            f"/v0/project/{project_name}/contexts?prefix={prefix}",
            headers=HEADERS,
        )
        assert response.status_code == 200
        filtered_contexts = response.json()
        assert len(filtered_contexts) == 3  # Each prefix has 3 contexts

        # Verify all returned contexts have the correct prefix
        for context in filtered_contexts:
            assert context["name"].startswith(prefix)

    # Test with a non-existent prefix
    response = await client.get(
        f"/v0/project/{project_name}/contexts?prefix=NonExistent/",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()) == 0  # Should return empty list


@pytest.mark.anyio
async def test_context_name_validation(client: AsyncClient):
    """Test that context names are properly validated"""
    project_name = "test-context-validation"

    # Create project
    await _create_project(client, project_name)

    # Test valid context names
    valid_names = [
        "valid-context",
        "valid_context",
        "valid/context",
        "valid123",
        "123valid",
        "UPPERCASE",
        "a" * 50,
    ]

    for name in valid_names:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": name, "description": f"Test context {name}"},
            headers=HEADERS,
        )
        assert (
            response.status_code == 200
        ), f"Expected 200 for name '{name}', got {response.status_code}: {response.json()}"
        assert "Context created successfully" in response.json()["info"]

    # Test invalid context names
    invalid_names = [
        "",  # Empty string
        " ",  # Just whitespace
        "invalid@context",  # Invalid character @
        "invalid#context",  # Invalid character #
        "invalid%context",  # Invalid character %
        "invalid.context",  # Invalid character .
        "invalid\ncontext",  # Newline
        "invalid\tcontext",  # Tab
    ]

    for name in invalid_names:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": name, "description": f"Test context {name}"},
            headers=HEADERS,
        )
        assert (
            response.status_code == 400
        ), f"Expected 400 for name '{name}', got {response.status_code}"
        assert "Invalid context name" in response.json()["detail"]


@pytest.mark.anyio
async def test_context_allow_duplicates(client: AsyncClient):
    """Test that contexts with allow_duplicates=False reject duplicate log entries"""
    project_name = "test-duplicate-prevention"

    # Create project
    await _create_project(client, project_name)

    # Create a context with allow_duplicates=False
    no_duplicates_context = "no-duplicates-context"
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": no_duplicates_context,
            "description": "Context that doesn't allow duplicates",
            "allow_duplicates": False,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a default context (allow_duplicates=True by default)
    default_context = "default-context"
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": default_context,
            "description": "Default context that allows duplicates",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a log with specific entries in the no-duplicates context
    log_data = {
        "project": project_name,
        "params": {"model": "gpt-4", "temperature": 0.7},
        "entries": {
            "accuracy": 0.95,
            "latency": 120,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "context": no_duplicates_context,
    }

    response = await client.post(
        "/v0/logs",
        json=log_data,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to create another log with identical key-value pairs - should be rejected
    response = await client.post(
        "/v0/logs",
        json=log_data,  # Same exact data
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Duplicate log detected" in response.json()["detail"]

    # Create a log with different values - should be accepted
    different_log = log_data.copy()
    different_log["entries"] = {
        "accuracy": 0.92,  # Different value
        "latency": 150,  # Different value
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    response = await client.post(
        "/v0/logs",
        json=different_log,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a log with same values but in the default context - should be accepted
    default_log = log_data.copy()
    default_log["context"] = default_context

    response = await client.post(
        "/v0/logs",
        json=default_log,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create another identical log in the default context - should still be accepted
    response = await client.post(
        "/v0/logs",
        json=default_log,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Test with different params but same entries - should be accepted
    different_params_log = log_data.copy()
    different_params_log["params"] = {"model": "gpt-3.5", "temperature": 0.5}

    response = await client.post(
        "/v0/logs",
        json=different_params_log,
        headers=HEADERS,
    )
    assert response.status_code == 200


# TODO: fix this test if we add support for duplicate context in update_logs endpoint.
# @pytest.mark.anyio
# async def test_context_duplicate_updates(client: AsyncClient):
#     """Test that updates which would create duplicates are rejected in contexts with allow_duplicates=False"""
#     project_name = "test-duplicate-updates"

#     # Create project
#     await _create_project(client, project_name)

#     # Create a context with allow_duplicates=False
#     no_duplicates_context = "no-duplicates-context"
#     response = await client.post(
#         f"/v0/project/{project_name}/contexts",
#         json={
#             "name": no_duplicates_context,
#             "description": "Context that doesn't allow duplicates",
#             "allow_duplicates": False,
#         },
#         headers=HEADERS,
#     )
#     assert response.status_code == 200

#     # Create a default context (allow_duplicates=True by default)
#     default_context = "default-context"
#     response = await client.post(
#         f"/v0/project/{project_name}/contexts",
#         json={
#             "name": default_context,
#             "description": "Default context that allows duplicates",
#         },
#         headers=HEADERS,
#     )
#     assert response.status_code == 200

#     # Create two logs with different values in the no-duplicates context
#     log_data_1 = {
#         "project": project_name,
#         "params": {"model": "gpt-4", "temperature": 0.7},
#         "entries": {
#             "accuracy": 0.95,
#             "latency": 120,
#             "timestamp": datetime.now(timezone.utc).isoformat(),
#         },
#         "context": no_duplicates_context,
#     }

#     log_data_2 = {
#         "project": project_name,
#         "params": {"model": "gpt-4", "temperature": 0.7},
#         "entries": {
#             "accuracy": 0.85,
#             "latency": 150,
#             "timestamp": datetime.now(timezone.utc).isoformat(),
#         },
#         "context": no_duplicates_context,
#     }

#     # Create first log
#     response = await _create_log(client, project_name, params=log_data_1["params"], entries=log_data_1["entries"], context=log_data_1["context"])
#     assert response.status_code == 200
#     log_id_1 = response.json()['log_event_ids'][0]

#     # Create second log
#     response = await _create_log(client, project_name, params=log_data_2["params"], entries=log_data_2["entries"], context=log_data_2["context"])
#     assert response.status_code == 200
#     log_id_2 = response.json()['log_event_ids'][0]

#     # Try to update the second log to have the same values as the first - should be rejected
#     update_response = await _update_logs(
#         client,
#         [log_id_2],
#         {"accuracy": 0.95, "latency": 120},
#         context=no_duplicates_context,
#         overwrite=True,
#     )
#     assert update_response.status_code == 400
#     assert "Duplicate log entry detected" in update_response.json()["detail"]

#     # Create two logs with different values in the default context
#     log_data_3 = {
#         "project": project_name,
#         "params": {"model": "gpt-3.5", "temperature": 0.5},
#         "entries": {
#             "accuracy": 0.90,
#             "latency": 100,
#             "timestamp": datetime.now(timezone.utc).isoformat(),
#         },
#         "context": default_context,
#     }

#     log_data_4 = {
#         "project": project_name,
#         "params": {"model": "gpt-3.5", "temperature": 0.5},
#         "entries": {
#             "accuracy": 0.80,
#             "latency": 130,
#             "timestamp": datetime.now(timezone.utc).isoformat(),
#         },
#         "context": default_context,
#     }

#     # Create third log
#     response = await _create_log(client, project_name, params=log_data_3["params"], entries=log_data_3["entries"], context=log_data_3["context"])
#     assert response.status_code == 200
#     log_id_3 = response.json()['log_event_ids'][0]

#     # Create fourth log
#     response = await _create_log(client, project_name, params=log_data_4["params"], entries=log_data_4["entries"], context=log_data_4["context"])
#     assert response.status_code == 200
#     log_id_4 = response.json()['log_event_ids'][0]

#     # Update the fourth log to have the same values as the third - should be accepted
#     # since the default context allows duplicates
#     update_response = await _update_logs(
#         client,
#         [log_id_4],
#         {"accuracy": 0.90, "latency": 100},
#         context=default_context,
#         overwrite=True,
#     )
#     assert update_response.status_code == 200
@pytest.mark.anyio
async def test_add_logs_with_copy_false(client: AsyncClient):
    """Test that when copy=false, the original logs are associated with the context"""
    project_name = "test-copy-false"
    context_name = "copy-false-context"

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

    # Create a log
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"model": "test-model"},
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_ids = response.json()["log_event_ids"]

    # Add logs to context with copy=false (default)
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={
            "context_name": context_name,
            "log_ids": log_ids,
            "copy": False,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Logs added to context successfully!" in response.json()["info"]

    # Verify the logs in the context have the same IDs as the original logs
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}&return_ids_only=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    context_log_ids = response.json()
    assert len(context_log_ids) == 1
    assert context_log_ids[0] == log_ids[0]  # Same ID as original log


@pytest.mark.anyio
async def test_add_logs_with_copy_true(client: AsyncClient):
    """Test that when copy=true, new log event ids are created and associated with the context"""
    project_name = "test-copy-true"
    context_name = "copy-true-context"

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

    # Create a log
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "metric": 0.95,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": "test-model",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    original_log_ids = response.json()["log_event_ids"]

    # Add logs to context with copy=true
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={
            "context_name": context_name,
            "log_ids": original_log_ids,
            "copy": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Logs added to context successfully!" in response.json()["info"]

    # Verify the logs in the context have different IDs than the original logs
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}&return_ids_only=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    context_log_ids = response.json()
    assert len(context_log_ids) == 1
    assert context_log_ids[0] != original_log_ids[0]  # Different ID than original log

    # Verify the content of the copied log is the same as the original
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    copied_logs = response.json()["logs"]
    assert len(copied_logs) == 1
    assert copied_logs[0]["entries"]["metric"] == 0.95
    assert copied_logs[0]["entries"]["model"] == "test-model"


@pytest.mark.anyio
async def test_add_logs_via_arguments(client: AsyncClient):
    """Test adding logs to a context using log_args instead of explicit log_ids"""
    project_name = "test-args-context"
    context_name = "args-context"

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

    # Create multiple logs with different metrics
    for metric_value in [0.85, 0.90, 0.95]:
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": {"model": "test-model"},
                "entries": {
                    "metric": metric_value,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

    # Add logs to context using log_args to filter for logs with metric > 0.9
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={
            "context_name": context_name,
            "log_args": {
                "filter_expr": "metric > 0.9",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Logs added to context successfully!" in response.json()["info"]

    # Verify only logs with metric > 0.9 were added to the context
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["metric"] == 0.95


@pytest.mark.anyio
async def test_add_logs_via_arguments_with_copy(client: AsyncClient):
    """Test adding logs to a context using log_args with copy=true"""
    project_name = "test-args-copy"
    context_name = "args-copy-context"

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

    # Create multiple logs with different metrics
    for metric_value in [0.85, 0.90, 0.95]:
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": {"model": "test-model"},
                "entries": {
                    "metric": metric_value,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

    # Get all logs to compare IDs later
    response = await client.get(
        f"/v0/logs?project={project_name}&return_ids_only=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    all_original_log_ids = set(response.json())

    # Add logs to context using log_args with copy=true
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={
            "context_name": context_name,
            "log_args": {
                "filter_expr": "metric > 0.85",
            },
            "copy": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Logs added to context successfully!" in response.json()["info"]

    # Verify logs in the context have different IDs than the original logs
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}&return_ids_only=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    context_log_ids = set(response.json())

    # Verify no overlap between original and copied log IDs
    assert len(context_log_ids.intersection(all_original_log_ids)) == 0

    # Verify the correct number of logs were copied (metric > 0.85 should be 2 logs)
    assert len(context_log_ids) == 2

    # Verify the content of the copied logs
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    copied_logs = response.json()["logs"]
    assert len(copied_logs) == 2

    # Check that the copied logs have the expected metric values
    metrics = [log["entries"]["metric"] for log in copied_logs]
    assert 0.90 in metrics
    assert 0.95 in metrics
