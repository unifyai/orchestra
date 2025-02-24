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
    log_id = log_response.json()[0]

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
    log_id = log_response.json()[0]

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
    log_id = log_response.json()[0]

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
    log_id = log_response.json()[0]

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
    log_id = log_response.json()[0]

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
    log_id = log_response.json()[0]

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
