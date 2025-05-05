import pytest
from httpx import AsyncClient

from . import (
    HEADERS,
    _create_log,
    _create_project,
    _get_log,
    _update_logs,
    _update_multiple_logs_w_overwrite,
    log_data,
)


@pytest.mark.anyio
async def test_update_logs_overwrites(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    response = await _create_log(client, project_name, entries=log_data["log"])
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 200, response.json()
    orig_entries = response.json()["logs"][0]["entries"]
    assert len(orig_entries) == 3

    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "entries": log_data["log_update"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_id_2 = response.json()["log_event_ids"][0]

    log_ids = [log_id, log_id_2]

    response = await _update_multiple_logs_w_overwrite(client, log_ids, overwrite=False)
    assert response.status_code == 400, response.json()

    response = await _update_multiple_logs_w_overwrite(client, log_ids, overwrite=True)
    assert response.status_code == 200, response.json()

    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 200, response.json()
    new_entries = response.json()["logs"][0]["entries"]
    assert len(new_entries) == 3
    assert new_entries["a/b/c/input"] == orig_entries["a/b/c/input"]
    assert new_entries["a/b/c/boolean_input"] != orig_entries["a/b/c/boolean_input"]
    assert new_entries["a/b/c/numeric_input"] != orig_entries["a/b/c/numeric_input"]

    response = await _get_log(client, project_name, log_id_2)
    assert response.status_code == 200, response.json()
    new_entries = response.json()["logs"][0]["entries"]
    assert len(new_entries) == 4


@pytest.mark.anyio
async def test_update_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()["log_event_ids"][0]
    log_id2 = response2.json()["log_event_ids"][0]
    log_ids = [log_id1, log_id2]

    # Update both logs
    entries = {
        "new_entry": "Updated value",
        "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
    }
    response = await _update_logs(client, log_ids, entries)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify updates
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "Updated value"

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "Updated value"


@pytest.mark.anyio
async def test_update_logs_multi_values(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()["log_event_ids"][0]
    log_id2 = response2.json()["log_event_ids"][0]
    log_ids = [log_id1, log_id2]

    # Update both logs
    entries = [
        {
            "new_entry": "First updated value",
            "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
        },
        {
            "new_entry": "Second updated value",
            "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
        },
    ]
    response = await _update_logs(client, log_ids, entries)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify updates
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "First updated value"

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "Second updated value"


@pytest.mark.anyio
async def test_update_logs_with_context_string(client: AsyncClient):
    """Test updating logs with context provided as a string."""
    project_name = "context-string-project"
    _ = await _create_project(client, project_name)

    # Create a context
    context_name = "test-context"
    # Create a log
    response = await _create_log(client, project_name, context=context_name)
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Update log with context as string
    entries = {
        "new_entry": "Updated with string context",
        "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
    }
    response = await _update_logs(client, [log_id], entries, context=context_name)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify update
    response = await client.get(
        f"/v0/logs?project={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert len(response.json()["logs"]) == 1
    assert (
        response.json()["logs"][0]["entries"]["new_entry"]
        == "Updated with string context"
    )


@pytest.mark.anyio
async def test_update_logs_with_context_list(client: AsyncClient):
    """Test updating logs with context provided as a list of strings."""
    project_name = "context-list-project"
    _ = await _create_project(client, project_name)

    # Create multiple contexts
    log_ids = []
    context_names = ["context1", "context2"]
    for context_name in context_names:
        # Create a log
        response = await _create_log(client, project_name, context=context_name)
        assert response.status_code == 200, response.json()
        log_ids.append(response.json()["log_event_ids"][0])

    # Update log with context as list of strings
    entries = {
        "new_entry": "Updated with list of contexts",
        "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
    }
    response = await _update_logs(client, log_ids, entries, context=context_names)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify update in both contexts
    for context_name in context_names:
        response = await client.get(
            f"/v0/logs?project={project_name}&context={context_name}",
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert len(response.json()["logs"]) == 1
        assert (
            response.json()["logs"][0]["entries"]["new_entry"]
            == "Updated with list of contexts"
        )


@pytest.mark.anyio
async def test_update_logs_by_value_filter(client: AsyncClient):
    """Test updating logs using a value filter instead of explicit IDs."""
    project_name = "filter-by-value-project"
    _ = await _create_project(client, project_name)

    # Create two logs with different status values
    done_log_entries = {
        "status": "done",
        "data": "This log is done",
        "explicit_types": {
            "status": {"type": "str", "mutable": True},
            "data": {"type": "str", "mutable": True},
        },
    }

    pending_log_entries = {
        "status": "pending",
        "data": "This log is pending",
        "explicit_types": {
            "status": {"type": "str", "mutable": True},
            "data": {"type": "str", "mutable": True},
        },
    }

    # Create the logs
    response_done = await _create_log(client, project_name, entries=done_log_entries)
    assert response_done.status_code == 200, response_done.json()
    done_log_id = response_done.json()["log_event_ids"][0]

    response_pending = await _create_log(
        client,
        project_name,
        entries=pending_log_entries,
    )
    assert response_pending.status_code == 200, response_pending.json()
    pending_log_id = response_pending.json()["log_event_ids"][0]

    # Update logs using value filter (status: done)
    update_entries = {
        "status": "complete",
        "explicit_types": {"status": {"type": "str", "mutable": True}},
    }

    # Use PUT request directly since _update_logs helper uses explicit IDs
    response = await client.put(
        "/v0/logs",
        json={
            "logs": {"status": "done"},
            "entries": update_entries,
            "project": project_name,
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify only the "done" log was updated to "complete"
    response_done_log = await _get_log(client, project_name, done_log_id)
    assert response_done_log.status_code == 200, response_done_log.json()
    assert response_done_log.json()["logs"][0]["entries"]["status"] == "complete"

    # Verify the "pending" log was not updated
    response_pending_log = await _get_log(client, project_name, pending_log_id)
    assert response_pending_log.status_code == 200, response_pending_log.json()
    assert response_pending_log.json()["logs"][0]["entries"]["status"] == "pending"


@pytest.mark.anyio
async def test_update_logs_filter_missing_project_or_context(client: AsyncClient):
    """Test that updating logs with a filter dict requires either project or context."""
    project_name = "missing-project-context-project"
    _ = await _create_project(client, project_name)

    # Create a log with status value
    log_entries = {
        "status": "active",
        "data": "This is test data",
        "explicit_types": {
            "status": {"type": "str", "mutable": True},
            "data": {"type": "str", "mutable": True},
        },
    }

    response = await _create_log(client, project_name, entries=log_entries)
    assert response.status_code == 200, response.json()

    # Update entries to use in the request
    update_entries = {
        "status": "updated",
        "explicit_types": {"status": {"type": "str", "mutable": True}},
    }

    # Send PUT request without project or context
    response = await client.put(
        "/v0/logs",
        json={
            "logs": {"status": "active"},  # Filter dict
            "entries": update_entries,
            # Deliberately omitting both project and context
        },
        headers=HEADERS,
    )

    # Assert that we get a 400 error
    assert response.status_code == 400, response.json()
    # Verify the error message matches the validation message
    assert (
        "When passing a filter dict in `logs`, you must supply `project`."
        in response.json()["detail"]
    )
