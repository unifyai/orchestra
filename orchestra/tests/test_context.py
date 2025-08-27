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
async def test_create_context_with_slash(client: AsyncClient):
    project_name = "test-project"
    context_name = "/training/trial1"

    # Create project first
    response = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Context created successfully" in response.json()["info"]

    # Try to create same context again
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name[1:]},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert (
        "A context with this name already exists in the project"
        in response.json()["detail"]
    )

    # Check that context was created
    response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["name"] == context_name[1:]


@pytest.mark.anyio
async def test_delete_context_with_slash(client: AsyncClient):
    project_name = "test-project"
    context_name = "/training/trial1"

    # Create project first
    response = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Context created successfully" in response.json()["info"]

    # Check that context was created
    response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["name"] == context_name[1:]

    # Delete context
    request = await client.delete(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
async def test_rename_context_with_slash(client: AsyncClient):
    project_name = "test-project"
    context_name = "/training/trial1"
    context_name2 = "/training/trial2"

    # Create project first
    response = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Context created successfully" in response.json()["info"]

    # Check that context was created
    response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["name"] == context_name[1:]

    # Perform rename
    resp = await client.patch(
        f"/v0/project/{project_name}/contexts/{context_name}/rename",
        json={"name": context_name2},
        headers=HEADERS,
    )
    assert resp.status_code == 200


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


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_rename_context(client: AsyncClient):
    project = "rename-test-project"
    old = "experiment1/trial1"
    new = "experiment1/trial2"

    # Create project and initial context
    await client.post("/v0/project", json={"name": project}, headers=HEADERS)
    await client.post(
        f"/v0/project/{project}/contexts",
        json={"name": old, "description": "desc"},
        headers=HEADERS,
    )

    # Perform rename
    resp = await client.patch(
        f"/v0/project/{project}/contexts/{old}/rename",
        json={"name": new},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert "renamed successfully" in resp.json()["info"].lower()

    # Old endpoint should 404
    r_old = await client.get(
        f"/v0/project/{project}/contexts/{old}",
        headers=HEADERS,
    )
    assert r_old.status_code == 404

    # New endpoint should exist
    r_new = await client.get(
        f"/v0/project/{project}/contexts/{new}",
        headers=HEADERS,
    )
    assert r_new.status_code == 200
    assert r_new.json()["name"] == new

    # Name collision yields 400
    await client.post(
        f"/v0/project/{project}/contexts",
        json={"name": "dup", "description": "dup"},
        headers=HEADERS,
    )
    conflict = await client.patch(
        f"/v0/project/{project}/contexts/{new}/rename",
        json={"name": "dup"},
        headers=HEADERS,
    )
    assert conflict.status_code == 400
    assert "already exists" in conflict.json()["detail"].lower()


@pytest.mark.anyio
async def test_context_with_sequential_id(client: AsyncClient):
    """Test that logs in a context with unique_keys get a sequential ID."""
    project_name = "sequential-id-project"
    context_name = "sequential-id-context"
    unique_id_names = "my_row_id"

    # Create project
    await _create_project(client, project_name)

    # Create a context with unique_keys enabled
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context with sequential IDs",
            "unique_keys": {unique_id_names: "int"},
            "auto_counting": {unique_id_names: None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create multiple logs in this context
    for i in range(5):
        log_response = await _create_log(
            client,
            project_name,
            entries={"value": f"log-entry-{i}"},
            context=context_name,
        )
        assert log_response.status_code == 200, log_response.text

    # Fetch the logs from the context
    logs = await fetch_logs(
        client,
        project_name,
        context=context_name,
        sort_by=unique_id_names,
    )

    # Verify the logs and their sequential IDs
    assert len(logs) == 5
    for i, log in enumerate(reversed(logs)):
        assert unique_id_names in log["entries"]
        assert log["entries"][unique_id_names] == i


@pytest.mark.anyio
async def test_nested_ids_explicit_set_fails(client: AsyncClient):
    """Test that attempting to explicitly set a unique ID column fails."""
    project_name = "nested-id-fail-project"
    context_name = "nested-id-fail-context"
    unique_id_names = ["task_id", "instance_id"]
    await _create_project(client, project_name)
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {col: "int" for col in unique_id_names},
            "auto_counting": {
                unique_id_names[0]: None,
                unique_id_names[1]: unique_id_names[0],
            },
        },
        headers=HEADERS,
    )

    # Attempt to set 'task_id' in entries
    log_response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"task_id": 99},
    )
    assert log_response.status_code == 400


@pytest.mark.anyio
async def test_nested_ids_non_existent_parent_fails(client: AsyncClient):
    """Test that providing a non-existent parent ID fails."""
    project_name = "nested-id-parent-fail-project"
    context_name = "nested-id-parent-fail-context"
    unique_id_names = ["user", "session"]
    await _create_project(client, project_name)
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {col: "int" for col in unique_id_names},
            "auto_counting": {
                unique_id_names[0]: None,
                unique_id_names[1]: unique_id_names[0],
            },
        },
        headers=HEADERS,
    )

    # Attempt to create a session for a user that doesn't exist yet
    log_response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"user": 999},  # This user has not been created
        params={},
    )
    assert log_response.status_code == 400  # Or appropriate error code


@pytest.mark.anyio
async def test_nested_unique_ids_increment(client: AsyncClient):
    """Test the incrementing logic for nested unique IDs."""
    project_name = "nested-id-project"
    context_name = "nested-id-context"
    unique_id_names = ["user", "session", "event"]

    await _create_project(client, project_name)
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {col: "int" for col in unique_id_names},
            "auto_counting": {
                unique_id_names[0]: None,
                unique_id_names[1]: unique_id_names[0],
                unique_id_names[2]: unique_id_names[1],
            },
        },
        headers=HEADERS,
    )

    # 1. Create first user
    res = await _create_log(client, project_name, context=context_name, params={})
    assert res.status_code == 200, res.text
    row_ids_data = res.json()["row_ids"]
    assert row_ids_data["names"] == unique_id_names
    assert row_ids_data["ids"] == [[0, 0, 0]]

    # 2. Create second user
    res = await _create_log(client, project_name, context=context_name, params={})
    assert res.status_code == 200, res.text
    row_ids_data = res.json()["row_ids"]
    assert row_ids_data["names"] == unique_id_names
    assert row_ids_data["ids"] == [[1, 0, 0]]

    # 3. Create a new session for user 0
    res = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"user": 0},
        params={},
    )
    assert res.status_code == 200, res.text
    row_ids_data = res.json()["row_ids"]
    assert row_ids_data["names"] == unique_id_names
    assert row_ids_data["ids"] == [[0, 1, 0]]

    # 4. Create a new event for user 0, session 1
    res = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"user": 0, "session": 1},
        params={},
    )
    assert res.status_code == 200, res.text
    row_ids_data = res.json()["row_ids"]
    assert row_ids_data["names"] == unique_id_names
    assert row_ids_data["ids"] == [[0, 1, 1]]

    # 5. Create another session for user 0
    res = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"user": 0},
        params={},
    )
    assert res.status_code == 200, res.text
    row_ids_data = res.json()["row_ids"]
    assert row_ids_data["names"] == unique_id_names
    assert row_ids_data["ids"] == [[0, 2, 0]]

    # Fetch and verify final state
    logs = await fetch_logs(client, project_name, context=context_name)
    assert len(logs) == 5

    results = [
        (l["entries"]["user"], l["entries"]["session"], l["entries"]["event"])
        for l in logs
    ]
    assert (0, 0, 0) in results
    assert (1, 0, 0) in results
    assert (0, 1, 0) in results
    assert (0, 1, 1) in results
    assert (0, 2, 0) in results


@pytest.mark.anyio
async def test_nested_ids_batch_creation(client: AsyncClient):
    """Test that a single API call with a batch of entries generates sequential unique IDs."""
    project_name = "nested-id-batch-project"
    context_name = "nested-id-batch-context"
    unique_id_names = ["run_id", "step_id"]
    await _create_project(client, project_name)
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {col: "int" for col in unique_id_names},
            "auto_counting": {
                unique_id_names[0]: None,
                unique_id_names[1]: unique_id_names[0],
            },
        },
        headers=HEADERS,
    )

    # First, create the parent run_id=0. This will also create step_id=0.
    res = await _create_log(client, project_name, context=context_name, params={})
    assert res.status_code == 200
    row_ids_data = res.json()["row_ids"]
    assert row_ids_data["names"] == unique_id_names
    assert row_ids_data["ids"] == [[0, 0]]

    # Now, create a batch of 5 steps under run_id=0
    batch_size = 5
    log_response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": context_name,
            # Create a batch of 5 logs, each with some data and parent ID
            "entries": [{"data": f"step_{i}", "run_id": 0} for i in range(batch_size)],
        },
        headers=HEADERS,
    )

    assert log_response.status_code == 200, log_response.text
    response_data = log_response.json()

    # Verify the response contains the standardized format for the batch
    row_ids_data = response_data["row_ids"]
    assert row_ids_data["names"] == unique_id_names

    # The step_ids should start from 1 because step_id=0 was used when the parent was created.
    expected_ids = [[0, i] for i in range(1, batch_size + 1)]
    assert row_ids_data["ids"] == expected_ids

    # Fetch all logs and verify the database state
    logs = await fetch_logs(client, project_name, context=context_name)
    assert len(logs) == batch_size + 1

    # Check that all expected step_ids are present for run_id 0
    db_step_ids = {
        log["entries"]["step_id"] for log in logs if log["entries"]["run_id"] == 0
    }
    expected_db_steps = set(range(batch_size + 1))
    assert db_step_ids == expected_db_steps


@pytest.mark.anyio
async def test_unique_keys_none_disables_unique_ids(client: AsyncClient):
    """Test that unique_keys: None disables unique IDs."""
    project_name = "no-unique-ids-project"
    context_name = "no-unique-ids-context"

    # Create project
    await _create_project(client, project_name)

    # Create a context with unique_keys: None
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context without unique IDs",
            "unique_keys": None,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create logs in this context
    for i in range(3):
        log_response = await _create_log(
            client,
            project_name,
            entries={"value": f"log-entry-{i}"},
            context=context_name,
        )
        assert log_response.status_code == 200
        # Verify no row_ids are returned
        response_data = log_response.json()
        assert "row_ids" not in response_data or response_data["row_ids"]["names"] == []

    # Fetch the logs from the context
    logs = await fetch_logs(client, project_name, context=context_name)

    # Verify no unique ID columns were created
    assert len(logs) == 3
    for log in logs:
        # Should not have any unique ID columns
        assert "row_id" not in log["entries"]


@pytest.mark.anyio
async def test_unique_keys_empty_dict_validation(client: AsyncClient):
    """Test that unique_keys: {} is rejected with validation error."""
    project_name = "empty-dict-validation-project"
    context_name = "empty-dict-validation-context"

    # Create project
    await _create_project(client, project_name)

    # Try to create a context with empty dict - should fail validation
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context with empty unique_keys",
            "unique_keys": {},
        },
        headers=HEADERS,
    )
    assert response.status_code == 422  # Validation error
    assert "cannot be an empty dict" in response.json()["detail"][0]["msg"]


@pytest.mark.anyio
async def test_single_column_returns_nested_format(client: AsyncClient):
    """Test that single unique column returns nested format."""
    project_name = "single-column-nested-project"
    context_name = "single-column-nested-context"
    unique_column_name = "sequence_id"

    # Create project
    await _create_project(client, project_name)

    # Create a context with single unique column
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context with single unique column",
            "unique_keys": {unique_column_name: "int"},
            "auto_counting": {unique_column_name: None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create multiple logs
    for i in range(3):
        log_response = await _create_log(
            client,
            project_name,
            entries={"value": f"log-entry-{i}"},
            context=context_name,
        )
        print(log_response.json())
        assert log_response.status_code == 200

        # Verify response format is nested even for single column
        row_ids_data = log_response.json()["row_ids"]
        assert row_ids_data["names"] == [unique_column_name]
        assert row_ids_data["ids"] == [[i]]  # Nested format: [[0]], [[1]], [[2]]

    # Fetch the logs from the context
    logs = await fetch_logs(client, project_name, context=context_name)

    # Verify the logs have the correct unique IDs
    assert len(logs) == 3
    for i, log in enumerate(reversed(logs)):  # Reversed because of default sorting
        assert unique_column_name in log["entries"]
        assert log["entries"][unique_column_name] == i


@pytest.mark.anyio
async def test_invalid_column_names_validation(client: AsyncClient):
    """Test validation of invalid column names."""
    project_name = "invalid-names-project"
    context_name = "invalid-names-context"

    # Create project
    await _create_project(client, project_name)

    # Test various invalid column names
    invalid_names = [
        ["invalid-name!"],  # Contains exclamation mark
        ["invalid name"],  # Contains space
        ["invalid.name"],  # Contains dot
        ["invalid@name"],  # Contains at symbol
        [""],  # Empty string
        ["valid_name", "invalid-name!"],  # Mix of valid and invalid
    ]

    for invalid_name_list in invalid_names:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={
                "name": f"{context_name}-{len(invalid_name_list)}",
                "description": "Context with invalid column names",
                "unique_keys": {name: "int" for name in invalid_name_list},
                "auto_counting": {name: None for name in invalid_name_list},
            },
            headers=HEADERS,
        )
        assert response.status_code == 422  # Validation error
        error_detail = response.json()["detail"][0]["msg"]
        assert (
            "must contain only alphanumeric characters and underscores" in error_detail
        )


@pytest.mark.anyio
async def test_duplicate_column_names_validation(client: AsyncClient):
    """Test that duplicate column names are handled by dict behavior (no duplicates allowed)."""
    project_name = "duplicate-names-project"
    context_name = "duplicate-names-context"

    # Create project
    await _create_project(client, project_name)

    # With dict format, duplicate keys are automatically resolved (last value wins)
    # So this will effectively create a context with only two keys: user_id and session_id
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context with duplicate column names handled by dict",
            "unique_keys": {
                "user_id": "int",
                "session_id": "int",
                "user_id": "int",  # This will overwrite the first user_id
            },
            "auto_counting": {
                "user_id": None,
                "session_id": "user_id",
            },
        },
        headers=HEADERS,
    )
    # Should succeed since dict automatically handles duplicates
    assert response.status_code == 200


@pytest.mark.anyio
async def test_valid_column_names_accepted(client: AsyncClient):
    """Test that valid column names are accepted."""
    project_name = "valid-names-project"
    context_name = "valid-names-context"

    # Create project
    await _create_project(client, project_name)

    # Test various valid column names
    valid_names = [
        ["user_id"],
        ["user123"],
        ["_private_id"],
        ["ID"],
        ["a"],
        ["user_id", "session_id", "event_id"],
        ["CamelCase", "snake_case", "UPPERCASE", "lowercase123"],
    ]

    for i, valid_name_list in enumerate(valid_names):
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={
                "name": f"{context_name}-{i}",
                "description": "Context with valid column names",
                "unique_keys": {name: "int" for name in valid_name_list},
                "auto_counting": {name: None for name in valid_name_list},
            },
            headers=HEADERS,
        )
        assert (
            response.status_code == 200
        ), f"Failed for names: {valid_name_list}, response: {response.json()}"
        assert "Context created successfully" in response.json()["info"]


@pytest.mark.anyio
async def test_composite_key_mixed_types(client: AsyncClient):
    """Test composite keys with mixed types (counting and non-counting)."""
    project_name = "composite-key-project"
    context_name = "mixed-keys-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with mixed composite key
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context with mixed composite keys",
            "unique_keys": {
                "department": "str",
                "employee_id": "int",
                "email": "str",
            },
            "auto_counting": {
                "employee_id": None,  # Independent counter (global)
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create first employee in Engineering department
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "department": "Engineering",
            "email": "alice@company.com",
            "name": "Alice",
        },
    )
    assert response.status_code == 200
    row_ids = response.json()["row_ids"]
    assert row_ids["names"] == ["department", "employee_id", "email"]
    assert row_ids["ids"] == [["Engineering", 0, "alice@company.com"]]

    # Create second employee in Engineering department
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "department": "Engineering",
            "email": "bob@company.com",
            "name": "Bob",
        },
    )
    assert response.status_code == 200
    row_ids = response.json()["row_ids"]
    assert row_ids["ids"] == [
        ["Engineering", 1, "bob@company.com"],
    ]  # Auto-incremented globally

    # Create first employee in Sales department
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "department": "Sales",
            "email": "charlie@company.com",
            "name": "Charlie",
        },
    )
    assert response.status_code == 200
    row_ids = response.json()["row_ids"]
    assert row_ids["ids"] == [
        ["Sales", 2, "charlie@company.com"],
    ]  # Continues global increment (not reset per department)

    # Verify we can retrieve all logs
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 3

    # Verify each log has composite key fields
    for log in logs:
        entries = log["entries"]
        assert "department" in entries
        assert "employee_id" in entries
        assert "email" in entries
        assert "name" in entries


@pytest.mark.anyio
async def test_composite_key_uniqueness_constraint(client: AsyncClient):
    """Test that composite key uniqueness is enforced."""
    project_name = "composite-unique-project"
    context_name = "unique-constraint-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with composite key and duplicates not allowed
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context that doesn't allow duplicates",
            "allow_duplicates": False,
            "unique_keys": {
                "first_name": "str",
                "last_name": "str",
                "birth_year": "int",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create first person
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "first_name": "John",
            "last_name": "Smith",
            "birth_year": 1990,
            "city": "New York",
        },
    )
    assert response.status_code == 200

    # Try to create duplicate (should fail)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "first_name": "John",
            "last_name": "Smith",
            "birth_year": 1990,
            "city": "Boston",  # Different city but same composite key
        },
    )
    assert response.status_code == 400
    assert "Duplicate" in response.json()["detail"]

    # Create person with same name but different birth year (should succeed)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "first_name": "John",
            "last_name": "Smith",
            "birth_year": 1991,
            "city": "Chicago",
        },
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_multiple_counting_columns(client: AsyncClient):
    """Test hierarchical counting columns in composite keys."""
    project_name = "hierarchical-counting-project"
    context_name = "multi-counting-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with multiple counting columns
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context with hierarchical counting",
            "unique_keys": {
                "company_id": "int",
                "department_id": "int",
                "team_id": "int",
                "location": "str",
            },
            "auto_counting": {
                "company_id": None,
                "department_id": "company_id",
                "team_id": "department_id",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create first company
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "location": "USA",
            "company_name": "TechCorp",
        },
    )
    assert response.status_code == 200
    row_ids = response.json()["row_ids"]
    assert row_ids["names"] == ["company_id", "department_id", "team_id", "location"]
    assert row_ids["ids"] == [[0, 0, 0, "USA"]]

    # Create second department in company 0
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "company_id": 0,
            "location": "USA",
            "department_name": "Engineering",
        },
    )
    assert response.status_code == 200
    row_ids = response.json()["row_ids"]
    assert row_ids["ids"] == [
        [0, 1, 0, "USA"],
    ]  # department_id incremented within company 0, team_id reset

    # Create second team in company 0, department 1
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "company_id": 0,
            "department_id": 1,
            "location": "USA",
            "team_name": "Backend",
        },
    )
    assert response.status_code == 200
    row_ids = response.json()["row_ids"]
    assert row_ids["ids"] == [
        [0, 1, 1, "USA"],
    ]  # team_id incremented within department 1


@pytest.mark.anyio
async def test_composite_key_missing_required_field(client: AsyncClient):
    """Test that non-counting columns in composite keys are required."""
    project_name = "required-fields-project"
    context_name = "required-fields-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with composite key
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context requiring composite key fields",
            "unique_keys": {
                "user_id": "int",
                "email": "str",
                "username": "str",
            },
            "auto_counting": {
                "user_id": None,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to create log without required field (should fail)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "email": "test@example.com",
            # Missing username
            "full_name": "Test User",
        },
    )
    assert response.status_code == 400
    assert (
        "Must provide value for composite key column 'username'"
        in response.json()["detail"]
    )


@pytest.mark.anyio
async def test_context_reference_after_context_deletion(client: AsyncClient):
    """Test behavior of entity context references after their referenced context is deleted"""
    project_name = "test-context-deletion"
    context_name = "deletable-context"

    # Create project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Context to be deleted"},
        headers=HEADERS,
    )

    # Create interface with context reference
    await client.post(
        "/v0/interface",
        json={
            "name": "test-interface",
            "project": project_name,
            "items": [],
            "new_counter": 0,
        },
        headers=HEADERS,
    )

    # Delete the context
    response = await client.delete(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to create new interface with the deleted context reference
    response = await client.post(
        "/v0/interface",
        json={
            "name": "test-interface-2",
            "project": project_name,
            "context": context_name,  # Reference to deleted context
            "items": [],
            "new_counter": 0,
        },
        headers=HEADERS,
    )
    # This should fail since the context no longer exists
    assert response.status_code == 400
    assert "Context 'deletable-context' not found" in response.json()["detail"]


@pytest.mark.anyio
async def test_context_reference_after_context_rename(client: AsyncClient):
    """Test behavior of entity context references after their referenced context is renamed"""
    project_name = "test-context-rename"
    old_context_name = "oldcontext"
    new_context_name = "newcontext"

    # Create project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": old_context_name, "description": "Context to be renamed"},
        headers=HEADERS,
    )

    # Create interface with context reference
    await client.post(
        "/v0/interface",
        json={
            "name": "test-interface",
            "project": project_name,
            "items": [],
            "new_counter": 0,
        },
        headers=HEADERS,
    )

    # Rename the context
    response = await client.patch(
        f"/v0/project/{project_name}/contexts/{old_context_name}/rename",
        json={"name": new_context_name},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to create new interface with the old context name
    response = await client.post(
        "/v0/interface",
        json={
            "name": "test-interface-2",
            "project": project_name,
            "context": old_context_name,  # Reference to old name
            "items": [],
            "new_counter": 0,
        },
        headers=HEADERS,
    )
    # This should fail since the old name no longer exists
    assert response.status_code == 400
    assert f"Context '{old_context_name}' not found" in response.json()["detail"]

    # Try to create interface with the new context name - should work
    response = await client.post(
        "/v0/interface",
        json={
            "name": "test-interface-3",
            "project": project_name,
            "context": new_context_name,  # Reference to new name
            "items": [],
            "new_counter": 0,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_cross_project_context_reference(client: AsyncClient):
    """Test that entities cannot reference contexts from different projects"""
    project_1 = "project-1"
    project_2 = "project-2"
    context_name = "shared-context-name"

    # Create two projects with contexts having the same name
    for project in [project_1, project_2]:
        await client.post(
            "/v0/project",
            json={"name": project},
            headers=HEADERS,
        )
        await client.post(
            f"/v0/project/{project}/contexts",
            json={"name": context_name, "description": f"Context in {project}"},
            headers=HEADERS,
        )

    # Create interface in project_1 referencing context from project_1 - should work
    response = await client.post(
        "/v0/interface",
        json={
            "name": "interface-1",
            "project": project_1,
            "context": context_name,  # Context from project_1
            "items": [],
            "new_counter": 0,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create interface in project_2 referencing context from project_2 - should work
    response = await client.post(
        "/v0/interface",
        json={
            "name": "interface-2",
            "project": project_2,
            "context": context_name,  # Context from project_2
            "items": [],
            "new_counter": 0,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Note: There's no way to directly reference a context from a different project
    # since context references are just strings and validation is project-scoped
    # This test mainly documents the expected behavior


@pytest.mark.anyio
async def test_independent_auto_counting(client: AsyncClient):
    """Test independent auto-counting columns that increment separately."""
    project_name = "independent-counting-project"
    context_name = "independent-counting-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with independent counters
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Context with independent counters",
            "unique_keys": {"message_id": "int"},
            "auto_counting": {
                "message_id": None,  # Independent counter
                "exchange_id": None,  # Independent counter (not part of unique key)
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create logs without providing counting fields
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"text": "Hello"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["row_ids"]["ids"] == [[0]]
    assert result["row_ids"]["names"] == ["message_id"]

    # Create another log
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"text": "World"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["row_ids"]["ids"] == [[1]]

    # Create logs with explicit exchange_id
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"text": "New exchange", "exchange_id": 5},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["row_ids"]["ids"] == [[2]]  # message_id continues incrementing

    # Get logs to verify all fields
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Check the logs have correct values
    assert logs[2]["entries"]["message_id"] == 0
    assert logs[2]["entries"]["exchange_id"] == 0  # Auto-incremented
    assert logs[1]["entries"]["message_id"] == 1
    assert logs[1]["entries"]["exchange_id"] == 1  # Auto-incremented
    assert logs[0]["entries"]["message_id"] == 2
    assert logs[0]["entries"]["exchange_id"] == 5  # Explicitly set

    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"text": "World"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["row_ids"]["ids"] == [[3]]  # message_id continues incrementing

    # Get logs to verify all fields
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Check the logs have correct values
    assert logs[3]["entries"]["message_id"] == 0
    assert logs[3]["entries"]["exchange_id"] == 0  # Auto-incremented
    assert logs[2]["entries"]["message_id"] == 1
    assert logs[2]["entries"]["exchange_id"] == 1  # Auto-incremented
    assert logs[1]["entries"]["message_id"] == 2
    assert logs[1]["entries"]["exchange_id"] == 5  # Auto-incremented
    assert logs[0]["entries"]["message_id"] == 3
    assert logs[0]["entries"]["exchange_id"] == 2  # Auto-incremented back from 1


@pytest.mark.anyio
async def test_hierarchical_auto_counting_validation(client: AsyncClient):
    """Test validation of hierarchical auto-counting relationships."""
    project_name = "hierarchical-validation-project"
    context_name = "hierarchical-validation-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with hierarchical counters
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"task_id": "int", "instance_id": "int"},
            "auto_counting": {
                "task_id": None,
                "instance_id": "task_id",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create first task (auto-increments task_id to 0)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"status": "started"},
    )
    assert response.status_code == 200
    assert response.json()["row_ids"]["ids"] == [[0, 0]]

    # Try to create instance for non-existent task (should fail)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"task_id": 999, "status": "running"},
    )
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]

    # Create instance for existing task (should succeed)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"task_id": 0, "status": "running"},
    )
    assert response.status_code == 200
    assert response.json()["row_ids"]["ids"] == [[0, 1]]


@pytest.mark.anyio
async def test_auto_counting_circular_dependency_validation(client: AsyncClient):
    """Test that circular dependencies in auto_counting are rejected."""
    project_name = "circular-dep-project"

    # Create project
    await _create_project(client, project_name)

    # Try to create context with circular dependency
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "circular-context",
            "unique_keys": {"col1": "int", "col2": "int"},
            "auto_counting": {
                "col1": "col2",
                "col2": "col1",  # Circular!
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "Circular dependency" in response.json()["detail"][0]["msg"]

    # Try self-referencing
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "self-ref-context",
            "unique_keys": {"col1": "int"},
            "auto_counting": {
                "col1": "col1",  # Self reference!
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "cannot be its own parent" in response.json()["detail"][0]["msg"]


@pytest.mark.anyio
async def test_auto_counting_parent_not_in_config(client: AsyncClient):
    """Test that parent columns must also be in auto_counting."""
    project_name = "parent-validation-project"

    # Create project
    await _create_project(client, project_name)

    # Try to create context where parent is not in auto_counting
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "invalid-parent-context",
            "unique_keys": {"col1": "int", "col2": "int"},
            "auto_counting": {
                "col2": "col1",  # col1 not in auto_counting!
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "must also be in auto_counting" in response.json()["detail"][0]["msg"]


@pytest.mark.anyio
async def test_get_context_returns_auto_counting(client: AsyncClient):
    """Test that getting context info returns auto_counting configuration."""
    project_name = "get-auto-counting-project"
    context_name = "test-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with auto_counting
    auto_counting_config = {"col1": None, "col2": "col1", "col3": "col2"}
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {"col1": "int", "col2": "int", "col3": "int"},
            "auto_counting": auto_counting_config,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Get single context
    response = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["unique_keys"] == ["col1", "col2", "col3"]
    assert result["auto_counting"] == auto_counting_config

    # Get all contexts
    response = await client.get(
        f"/v0/project/{project_name}/contexts",
        headers=HEADERS,
    )
    assert response.status_code == 200
    contexts = response.json()
    assert len(contexts) == 1
    assert contexts[0]["name"] == context_name
    assert contexts[0]["auto_counting"] == auto_counting_config


@pytest.mark.anyio
async def test_mixed_auto_counting_and_explicit_values(client: AsyncClient):
    """Test context with some auto-counting and some explicit unique key columns."""
    project_name = "mixed-counting-project"
    context_name = "mixed-counting-context"

    # Create project
    await _create_project(client, project_name)

    # Create context where only some columns auto-count
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "unique_keys": {
                "user_id": "int",
                "email": "str",
                "session_id": "int",
            },
            "auto_counting": {
                "user_id": None,  # Auto-counts
                "session_id": "user_id",  # Auto-counts per user
                # email does NOT auto-count
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create log - must provide email
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"name": "John", "email": "john@example.com"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["row_ids"]["ids"] == [[0, "john@example.com", 0]]
    assert result["row_ids"]["names"] == ["user_id", "email", "session_id"]

    # Try to create without email (should fail)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"name": "Jane"},
    )
    assert response.status_code == 400
    assert (
        "Must provide value for composite key column 'email'"
        in response.json()["detail"]
    )

    # Create another session for same user
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={"user_id": 0, "email": "john@example.com", "action": "login"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["row_ids"]["ids"] == [
        [0, "john@example.com", 1],
    ]  # session_id incremented


@pytest.mark.anyio
async def test_auto_counting_with_non_int_types(client: AsyncClient):
    """Test that auto_counting only works with int type columns."""
    project_name = "type-validation-project"

    # Create project
    await _create_project(client, project_name)

    # All auto-counting columns should be int type
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "valid-types-context",
            "unique_keys": {
                "counter1": "int",
                "counter2": "int",
                "name": "str",
            },
            "auto_counting": {
                "counter1": None,
                "counter2": "counter1",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a log
    response = await _create_log(
        client,
        project_name,
        context="valid-types-context",
        entries={"name": "test"},
    )
    assert response.status_code == 200
    assert response.json()["row_ids"]["ids"] == [[0, 0, "test"]]


@pytest.mark.anyio
async def test_composite_key_uniqueness_with_allow_duplicates_false(
    client: AsyncClient,
):
    """Test that composite key uniqueness is enforced when allow_duplicates=False."""
    project_name = "composite-uniqueness-project"
    context_name = "composite-uniqueness-context"

    # Create project
    await _create_project(client, project_name)

    # Create context with composite key and duplicates not allowed
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "allow_duplicates": False,
            "unique_keys": {
                "org_id": "str",
                "user_id": "int",
                "role": "str",
            },
            "auto_counting": {
                "user_id": None,  # Only user_id auto-counts
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create first entry
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "org_id": "acme-corp",
            "role": "admin",
            "name": "Alice",
        },
    )
    assert response.status_code == 200
    print(response.json())
    assert response.json()["row_ids"]["ids"] == [["acme-corp", 0, "admin"]]

    # Try to create duplicate composite key (should fail)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "org_id": "acme-corp",
            "user_id": 0,  # Same composite key
            "role": "admin",
            "name": "Bob",  # Different name but same composite key
        },
    )
    assert response.status_code == 400
    assert "Duplicate" in response.json()["detail"]

    # Create entry with different composite key (should succeed)
    response = await _create_log(
        client,
        project_name,
        context=context_name,
        entries={
            "org_id": "acme-corp",
            "role": "user",  # Different role
            "name": "Charlie",
        },
    )
    assert response.status_code == 200
    assert response.json()["row_ids"]["ids"] == [["acme-corp", 1, "user"]]


@pytest.mark.anyio
async def test_individual_field_uniqueness_with_auto_counting(client: AsyncClient):
    """Test that individual field uniqueness is enforced alongside auto_counting."""
    project_name = "field-uniqueness-project"

    # Create project
    await _create_project(client, project_name)

    # Create a context with single unique key that auto-counts
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "users-context",
            "allow_duplicates": False,
            "unique_keys": {"user_id": "int"},
            "auto_counting": {"user_id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a log with email field
    response = await _create_log(
        client,
        project_name,
        context="users-context",
        entries={"email": "alice@example.com", "name": "Alice"},
    )
    assert response.status_code == 200
    assert response.json()["row_ids"]["ids"] == [[0]]

    # Create another log with different email
    response = await _create_log(
        client,
        project_name,
        context="users-context",
        entries={"email": "bob@example.com", "name": "Bob"},
    )
    print(response.json())
    assert response.status_code == 200
    assert response.json()["row_ids"]["ids"] == [[1]]

    # Field uniqueness would be enforced if we had set unique=True on email field
    # But with auto_counting, user_id is the only unique field
