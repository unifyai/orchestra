import pytest
from httpx import AsyncClient

from . import (
    HEADERS,
    _create_log,
    _create_project,
    _delete_log_fields_from_logs,
    _delete_logs,
    _get_log,
    _update_logs,
)


@pytest.mark.anyio
async def test_delete_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs (using the default context)
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()["log_event_ids"][0]
    log_id2 = response2.json()["log_event_ids"][0]
    ids_and_fields = [([log_id1, log_id2], None)]

    # create a new context
    context_name = "test-context"
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name},
        headers=HEADERS,
    )
    # add logs to the new context
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": context_name, "log_ids": [log_id1, log_id2]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Delete the logs
    response = await _delete_logs(client, ids_and_fields, project_name=project_name)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify logs were deleted
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json() == {"params": {}, "logs": [], "count": 0}

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json() == {"params": {}, "logs": [], "count": 0}


@pytest.mark.anyio
async def test_delete_field_for_all_logs(client: AsyncClient):
    """Test deleting a specific field from all logs when log ID is None."""
    project_name = "delete-field-all-logs"
    _ = await _create_project(client, project_name)

    # Create multiple logs with a common field
    common_field = "common/test/field"
    entries1 = {
        common_field: "value1",
        "unique/field1": "unique1",
        "explicit_types": {
            common_field: {"mutable": True},
            "unique/field1": {"mutable": True},
        },
    }
    entries2 = {
        common_field: "value2",
        "unique/field2": "unique2",
        "explicit_types": {
            common_field: {"mutable": True},
            "unique/field2": {"mutable": True},
        },
    }
    entries3 = {
        common_field: "value3",
        "unique/field3": "unique3",
        "explicit_types": {
            common_field: {"mutable": True},
            "unique/field3": {"mutable": True},
        },
    }

    response1 = await _create_log(client, project_name, entries=entries1)
    response2 = await _create_log(client, project_name, entries=entries2)
    response3 = await _create_log(client, project_name, entries=entries3)

    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()
    assert response3.status_code == 200, response3.json()

    log_id1 = response1.json()["log_event_ids"][0]
    log_id2 = response2.json()["log_event_ids"][0]
    log_id3 = response3.json()["log_event_ids"][0]

    # Verify logs were created with the common field
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 3

    for log in logs:
        assert common_field in log["entries"]

    # Delete the common field from all logs by passing None as log ID
    ids_and_fields = [(None, common_field)]
    response = await _delete_log_fields_from_logs(
        client,
        ids_and_fields,
        project_name=project_name,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify the field was removed from all logs
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 3

    for log in logs:
        assert common_field not in log["entries"]
        # Unique fields should still be present
        if log["id"] == log_id1:
            assert "unique/field1" in log["entries"]
        elif log["id"] == log_id2:
            assert "unique/field2" in log["entries"]
        elif log["id"] == log_id3:
            assert "unique/field3" in log["entries"]

    # Check field types to verify the field type was removed
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert common_field not in fields


@pytest.mark.anyio
async def test_field_cascaded_delete(client: AsyncClient):
    """Test that when a field is deleted from all logs, it is also removed from the field type table."""
    project_name = "field-cascaded-delete"
    _ = await _create_project(client, project_name)

    # Create a log with multiple fields
    test_field = "test/cascaded/field"
    other_field = "test/other/field"

    entries = {
        test_field: "test value",
        other_field: "other value",
        "explicit_types": {
            test_field: {"mutable": True},
            other_field: {"mutable": True},
        },
    }

    response = await _create_log(client, project_name, entries=entries)
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Create a second log with only the other field
    entries2 = {
        other_field: "second log value",
        "explicit_types": {
            other_field: {"mutable": True},
        },
    }

    response = await _create_log(client, project_name, entries=entries2)
    assert response.status_code == 200, response.json()
    log_id2 = response.json()["log_event_ids"][0]

    # Delete the test field from the first log
    # This should trigger cascaded deletion of the field type since no logs will have this field
    ids_and_fields = [(log_id, test_field)]
    response = await _delete_log_fields_from_logs(
        client,
        ids_and_fields,
        project_name=project_name,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify the field was removed from the log
    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 200, response.json()
    log_data = response.json()["logs"][0]
    assert test_field not in log_data["entries"]
    assert other_field in log_data["entries"]

    # Check that the other log still has its field
    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    log_data = response.json()["logs"][0]
    assert other_field in log_data["entries"]

    # Check that the field type was removed
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert test_field not in fields


@pytest.mark.anyio
async def test_delete_log_fields_from_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()["log_event_ids"][0]
    log_id2 = response2.json()["log_event_ids"][0]
    entry_to_delete = "a/b/c/input"
    ids_and_fields = [(log_id1, entry_to_delete), (log_id2, entry_to_delete)]

    # Delete entries from the logs
    response = await _delete_log_fields_from_logs(
        client,
        ids_and_fields,
        project_name=project_name,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify deletion of entry
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert entry_to_delete not in response.json()["logs"][0]["entries"]

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert entry_to_delete not in response.json()["logs"][0]["entries"]

    ids_and_fields = [
        (log_id1, ["a/b/c/boolean_input", "a/b/c/numeric_input", "a/b/param1"]),
        ([log_id1, log_id2], ["a/b/c/boolean_input", "a/b/param1"]),
    ]
    # Delete entries from the logs
    response = await _delete_log_fields_from_logs(
        client,
        ids_and_fields,
        delete_empty_logs=True,
        project_name=project_name,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    del result["logs"][0]["ts"]
    assert result["logs"] == [
        {
            "id": 2,
            "entries": {"a/b/c/numeric_input": 4.5},
            "params": {},
            "derived_entries": {},
            "versions": {},
            "clipped_fields": [],
        },
    ]


@pytest.mark.anyio
async def test_delete_logs_from_specific_context(client: AsyncClient):
    """Test deleting logs from a specific context while preserving them in other contexts."""
    project_name = "context-specific-deletion"
    _ = await _create_project(client, project_name)

    # Create two contexts
    context1 = "TestSet"
    context2 = "TestSetSmall"

    # Create a log (in context1)
    response = await _create_log(client, project_name, context=context1)
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    assert response.status_code == 200, response.json()

    # Create second context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Add log to the second context
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": context2, "log_ids": [log_id]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify log is in both contexts
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"context": context1},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id in [log["id"] for log in response.json()["logs"]]

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"context": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id in [log["id"] for log in response.json()["logs"]]

    # Delete the log from the first context only
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=context1,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify log is removed from first context
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"context": context1},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id not in [log["id"] for log in response.json()["logs"]]

    # Verify log is still in second context
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"context": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id in [log["id"] for log in response.json()["logs"]]

    # Delete the log from the second context
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=context2,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify log is also removed from second context
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"context": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_delete_project_deletes_logs(client: AsyncClient):
    url = "/v0/project/test-project"
    project_name = "test-project"

    # Create a project first to delete it
    # check that existing projects don't change the functionality
    _ = await _create_project(client, project_name, user=2)
    create_response = await _create_project(client, project_name)
    assert create_response.status_code == 200

    # add a log
    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]
    assert isinstance(log_id, int)

    # verify it exists
    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 200, response.json()

    # Now delete the project
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project deleted successfully"

    # Verify the log has gone
    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found.",
    }


@pytest.mark.anyio
async def test_delete_logs_by_value_filter(client: AsyncClient):
    """Test deleting logs by value filter instead of explicit IDs."""
    project_name = "filter-deletion-test"
    _ = await _create_project(client, project_name)

    # Create two logs with different tags
    keep_entries = {
        "tag": "keep",
        "data": "This log should be kept",
        "explicit_types": {
            "tag": {"mutable": True},
            "data": {"mutable": True},
        },
    }

    remove_entries = {
        "tag": "remove",
        "data": "This log should be removed",
        "explicit_types": {
            "tag": {"mutable": True},
            "data": {"mutable": True},
        },
    }

    # Create the logs
    response_keep = await _create_log(client, project_name, entries=keep_entries)
    response_remove = await _create_log(client, project_name, entries=remove_entries)

    assert response_keep.status_code == 200, response_keep.json()
    assert response_remove.status_code == 200, response_remove.json()

    keep_log_id = response_keep.json()["log_event_ids"][0]
    remove_log_id = response_remove.json()["log_event_ids"][0]

    # Verify both logs exist
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 2
    log_ids = [log["id"] for log in logs]
    assert keep_log_id in log_ids
    assert remove_log_id in log_ids

    # Delete logs with tag="remove" using value filter
    ids_and_fields = [({"tag": "remove"}, None)]
    response = await _delete_logs(client, ids_and_fields, project_name=project_name)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify only the "keep" log remains
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["id"] == keep_log_id
    assert logs[0]["entries"]["tag"] == "keep"

    # Verify the "remove" log is gone
    response = await _get_log(client, project_name, remove_log_id)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"] == []


@pytest.mark.anyio
async def test_delete_empty_fields_flag(client: AsyncClient):
    """Test that the delete_empty_fields flag controls whether fields are removed when no logs use them."""
    project_name = "empty-fields-test"
    _ = await _create_project(client, project_name)

    # Create two logs with a shared column
    shared_field = "shared/test/field"
    unique_field1 = "unique/test/field1"
    unique_field2 = "unique/test/field2"

    entries1 = {
        shared_field: "value1",
        unique_field1: "unique1",
        "explicit_types": {
            shared_field: {"mutable": True},
            unique_field1: {"mutable": True},
        },
    }

    entries2 = {
        shared_field: "value2",
        unique_field2: "unique2",
        "explicit_types": {
            shared_field: {"mutable": True},
            unique_field2: {"mutable": True},
        },
    }

    response1 = await _create_log(client, project_name, entries=entries1)
    response2 = await _create_log(client, project_name, entries=entries2)

    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()["log_event_ids"][0]
    log_id2 = response2.json()["log_event_ids"][0]

    # Verify logs were created with the shared column
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 2

    for log in logs:
        assert shared_field in log["entries"]

    # Test 1: Delete the shared column with delete_empty_fields=False
    ids_and_fields = [(None, shared_field)]
    response = await _delete_log_fields_from_logs(
        client,
        ids_and_fields,
        delete_empty_fields=False,
        project_name=project_name,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify the field was removed from logs but still exists in fields list
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    for log in logs:
        assert shared_field not in log["entries"]

    # Check that the column still exists in the columns list
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert shared_field in fields

    # Test 2: Re-create logs with the shared column
    await _update_logs(
        client,
        [log_id1],
        {shared_field: "value1", "explicit_types": {shared_field: {"mutable": True}}},
    )
    await _update_logs(
        client,
        [log_id2],
        {shared_field: "value2", "explicit_types": {shared_field: {"mutable": True}}},
    )

    # Verify logs again have the shared column
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    for log in logs:
        assert shared_field in log["entries"]

    # Test 3: Delete the shared column with delete_empty_columns=True
    ids_and_fields = [(None, shared_field)]
    response = await _delete_log_fields_from_logs(
        client,
        ids_and_fields,
        delete_empty_fields=True,
        project_name=project_name,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify the column was removed from logs AND from columns list
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    for log in logs:
        assert shared_field not in log["entries"]

    # Check that the column no longer exists in the columns list
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert shared_field not in fields


@pytest.mark.anyio
async def test_delete_all_logs_removes_all_fields_when_empty(client: AsyncClient):
    """Test that deleting all logs with delete_empty_fields=True removes all unused fields."""
    project_name = "delete-all-logs-fields-test"
    _ = await _create_project(client, project_name)

    num_logs = 5

    # Create multiple logs with various fields
    log_ids = []
    for i in range(num_logs):
        entries = {
            "grades/maths": 80 + i,
            "topics/physics": 70 + i,
            "topics/chemistry": 60 + i,
            "x": i,
            "y": num_logs - i,
            "explicit_types": {
                "grades/maths": {"mutable": True},
                "topics/physics": {"mutable": True},
                "topics/chemistry": {"mutable": True},
                "x": {"mutable": True},
                "y": {"mutable": True},
            },
        }
        response = await _create_log(client, project_name, entries=entries)
        assert response.status_code == 200, response.json()
        log_ids.append(response.json()["log_event_ids"][0])

    # Verify logs were created
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    original_logs = response.json()["logs"]
    assert len(original_logs) == num_logs

    # Verify fields were created
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    original_fields = response.json()
    expected_fields = ["grades/maths", "topics/physics", "topics/chemistry", "x", "y"]
    for field in expected_fields:
        assert field in original_fields

    # Delete all logs using the format [[log_id, None], ...]
    ids_and_fields = [[log_id, None] for log_id in log_ids]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        delete_empty_fields=True,
        delete_empty_logs=True,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify all logs were deleted
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    remaining_logs = response.json()["logs"]
    assert len(remaining_logs) == 0

    # Verify all fields were deleted since no logs remain
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    remaining_fields = response.json()
    for field in expected_fields:
        assert (
            field not in remaining_fields
        ), f"Field '{field}' should have been deleted but still exists"


@pytest.mark.anyio
async def test_delete_some_logs_keeps_fields_used_by_remaining_logs(
    client: AsyncClient,
):
    """Test that deleting some logs with delete_empty_fields=True keeps fields used by remaining logs but deletes unused fields."""
    project_name = "partial-log-deletion-field-test"
    _ = await _create_project(client, project_name)

    # Create logs with overlapping fields
    # Log 1: has fields A, B, C
    entries1 = {
        "field/A": "value_A1",
        "field/B": "value_B1",
        "field/C": "value_C1",
        "explicit_types": {
            "field/A": {"mutable": True},
            "field/B": {"mutable": True},
            "field/C": {"mutable": True},
        },
    }

    # Log 2: has fields A, B, D
    entries2 = {
        "field/A": "value_A2",
        "field/B": "value_B2",
        "field/D": "value_D2",
        "explicit_types": {
            "field/A": {"mutable": True},
            "field/B": {"mutable": True},
            "field/D": {"mutable": True},
        },
    }

    # Log 3: has fields C, E (will be deleted)
    entries3 = {
        "field/C": "value_C3",
        "field/E": "value_E3",
        "explicit_types": {
            "field/C": {"mutable": True},
            "field/E": {"mutable": True},
        },
    }

    response1 = await _create_log(client, project_name, entries=entries1)
    response2 = await _create_log(client, project_name, entries=entries2)
    response3 = await _create_log(client, project_name, entries=entries3)

    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()
    assert response3.status_code == 200, response3.json()

    log_id1 = response1.json()["log_event_ids"][0]
    log_id2 = response2.json()["log_event_ids"][0]
    log_id3 = response3.json()["log_event_ids"][0]

    # Verify all fields were created
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    original_fields = response.json()
    all_fields = ["field/A", "field/B", "field/C", "field/D", "field/E"]
    for field in all_fields:
        assert field in original_fields

    # Delete log3 (has field/C and field/E)
    # After deletion:
    # - field/A should remain (used by log1, log2)
    # - field/B should remain (used by log1, log2)
    # - field/C should remain (used by log1)
    # - field/D should remain (used by log2)
    # - field/E should be deleted (only used by log3)
    ids_and_fields = [[log_id3, None]]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        delete_empty_fields=True,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify log3 was deleted
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    remaining_logs = response.json()["logs"]
    assert len(remaining_logs) == 2
    remaining_log_ids = [log["id"] for log in remaining_logs]
    assert log_id1 in remaining_log_ids
    assert log_id2 in remaining_log_ids
    assert log_id3 not in remaining_log_ids

    # Verify field deletion behavior
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    remaining_fields = response.json()

    # These fields should still exist (used by remaining logs)
    fields_that_should_remain = ["field/A", "field/B", "field/C", "field/D"]
    for field in fields_that_should_remain:
        assert (
            field in remaining_fields
        ), f"Field '{field}' should still exist but was deleted"

    # This field should be deleted (only used by deleted log3)
    assert (
        "field/E" not in remaining_fields
    ), "Field 'field/E' should have been deleted but still exists"


@pytest.mark.anyio
async def test_delete_logs_keeps_fields_used_by_other_contexts(client: AsyncClient):
    """Test that deleting logs with delete_empty_fields=True keeps fields used by logs in other contexts."""
    project_name = "cross-context-field-deletion-test"
    _ = await _create_project(client, project_name)

    # Create two contexts
    context1 = "context1"
    context2 = "context2"

    # Create logs in context1 with certain fields
    entries_ctx1_log1 = {
        "shared/field": "ctx1_value1",
        "context1/unique": "ctx1_unique1",
        "will/be/orphaned": "orphaned_value",
        "explicit_types": {
            "shared/field": {"mutable": True},
            "context1/unique": {"mutable": True},
            "will/be/orphaned": {"mutable": True},
        },
    }

    entries_ctx1_log2 = {
        "shared/field": "ctx1_value2",
        "will/be/orphaned": "orphaned_value2",
        "explicit_types": {
            "shared/field": {"mutable": True},
            "will/be/orphaned": {"mutable": True},
        },
    }

    # Create logs in context1
    response1 = await _create_log(
        client,
        project_name,
        entries=entries_ctx1_log1,
        context=context1,
    )
    response2 = await _create_log(
        client,
        project_name,
        entries=entries_ctx1_log2,
        context=context1,
    )
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    ctx1_log_id1 = response1.json()["log_event_ids"][0]
    ctx1_log_id2 = response2.json()["log_event_ids"][0]

    # Create context2 and logs in context2 with overlapping fields
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    entries_ctx2 = {
        "shared/field": "ctx2_value",
        "context2/unique": "ctx2_unique",
        "explicit_types": {
            "shared/field": {"mutable": True},
            "context2/unique": {"mutable": True},
        },
    }

    response3 = await _create_log(
        client,
        project_name,
        entries=entries_ctx2,
        context=context2,
    )
    assert response3.status_code == 200, response3.json()
    ctx2_log_id = response3.json()["log_event_ids"][0]

    # Verify all fields exist
    response = await client.get(
        f"/v0/logs/fields?project={project_name}&context={context1}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx1_fields = response.json()
    all_fields = ["shared/field", "context1/unique", "will/be/orphaned"]
    for field in all_fields:
        assert field in ctx1_fields

    response = await client.get(
        f"/v0/logs/fields?project={project_name}&context={context2}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx2_fields = response.json()
    all_fields = ["shared/field", "context2/unique"]
    for field in all_fields:
        assert field in ctx2_fields

    # Delete all logs from context1
    # After deletion:
    # - "shared/field" should remain (used by context2)
    # - "context2/unique" should remain (used by context2)
    # - "context1/unique" should be deleted (only used by context1)
    # - "will/be/orphaned" should be deleted (only used by context1)
    ids_and_fields = [[ctx1_log_id1, None], [ctx1_log_id2, None]]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=context1,
        delete_empty_fields=True,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify logs were deleted from context1
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"context": context1},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx1_logs = response.json()["logs"]
    assert len(ctx1_logs) == 0

    # Verify logs still exist in context2
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"context": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx2_logs = response.json()["logs"]
    assert len(ctx2_logs) == 1
    assert ctx2_logs[0]["id"] == ctx2_log_id

    # Verify field deletion behavior across contexts
    response = await client.get(
        f"/v0/logs/fields?project={project_name}&context={context2}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx2_fields = response.json()

    # These fields should still exist (used by context2)
    fields_that_should_remain = ["shared/field", "context2/unique"]
    for field in fields_that_should_remain:
        assert (
            field in ctx2_fields
        ), f"Field '{field}' should still exist but was deleted"

    # These fields should be deleted (only used by deleted context1 logs)
    response = await client.get(
        f"/v0/logs/fields?project={project_name}&context={context1}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx1_fields = response.json()
    fields_that_should_be_deleted = ["context1/unique", "will/be/orphaned"]
    for field in fields_that_should_be_deleted:
        assert (
            field not in ctx1_fields
        ), f"Field '{field}' should have been deleted but still exists"
