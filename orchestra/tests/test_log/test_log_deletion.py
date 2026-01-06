import pytest
from httpx import AsyncClient

from orchestra.conftest import assert_mode_specific

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
async def test_delete_logs(client: AsyncClient, use_jsonb_mode):
    """Test deleting logs."""
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
async def test_delete_field_for_all_logs(client: AsyncClient, use_jsonb_mode):
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
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
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert common_field not in fields


@pytest.mark.anyio
async def test_field_cascaded_delete(client: AsyncClient, use_jsonb_mode):
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
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert test_field not in fields


@pytest.mark.anyio
async def test_delete_log_fields_from_logs(client: AsyncClient, use_jsonb_mode):
    """Test deleting specific fields from specific logs."""
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

    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
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
async def test_delete_logs_from_specific_context(client: AsyncClient, use_jsonb_mode):
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
        f"/v0/logs?project_name={project_name}",
        params={"context": context1},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id in [log["id"] for log in response.json()["logs"]]

    response = await client.get(
        f"/v0/logs?project_name={project_name}",
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
        f"/v0/logs?project_name={project_name}",
        params={"context": context1},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id not in [log["id"] for log in response.json()["logs"]]

    # Verify log is still in second context
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
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
        f"/v0/logs?project_name={project_name}",
        params={"context": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_delete_project_deletes_logs(client: AsyncClient, use_jsonb_mode):
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
async def test_delete_logs_by_value_filter(client: AsyncClient, use_jsonb_mode):
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
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
async def test_delete_empty_fields_flag(client: AsyncClient, use_jsonb_mode):
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    for log in logs:
        assert shared_field not in log["entries"]

    # Check that the column still exists in the columns list
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    for log in logs:
        assert shared_field not in log["entries"]

    # Check that the column no longer exists in the columns list
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert shared_field not in fields


@pytest.mark.anyio
async def test_delete_all_logs_removes_all_fields_when_empty(
    client: AsyncClient,
    use_jsonb_mode,
):
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    original_logs = response.json()["logs"]
    assert len(original_logs) == num_logs

    # Verify fields were created
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    remaining_logs = response.json()["logs"]
    assert len(remaining_logs) == 0

    # Verify all fields were deleted since no logs remain
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
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
    use_jsonb_mode,
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
        f"/v0/logs/fields?project_name={project_name}",
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
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    remaining_logs = response.json()["logs"]
    assert len(remaining_logs) == 2
    remaining_log_ids = [log["id"] for log in remaining_logs]
    assert log_id1 in remaining_log_ids
    assert log_id2 in remaining_log_ids
    assert log_id3 not in remaining_log_ids

    # Verify field deletion behavior
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
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
async def test_delete_logs_keeps_fields_used_by_other_contexts(
    client: AsyncClient,
    use_jsonb_mode,
):
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
        f"/v0/logs/fields?project_name={project_name}&context={context1}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx1_fields = response.json()
    all_fields = ["shared/field", "context1/unique", "will/be/orphaned"]
    for field in all_fields:
        assert field in ctx1_fields

    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context2}",
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
        f"/v0/logs?project_name={project_name}",
        params={"context": context1},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx1_logs = response.json()["logs"]
    assert len(ctx1_logs) == 0

    # Verify logs still exist in context2
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"context": context2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx2_logs = response.json()["logs"]
    assert len(ctx2_logs) == 1
    assert ctx2_logs[0]["id"] == ctx2_log_id

    # Verify field deletion behavior across contexts
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context2}",
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
        f"/v0/logs/fields?project_name={project_name}&context={context1}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    ctx1_fields = response.json()
    fields_that_should_be_deleted = ["context1/unique", "will/be/orphaned"]
    for field in fields_that_should_be_deleted:
        assert (
            field not in ctx1_fields
        ), f"Field '{field}' should have been deleted but still exists"


# =============================================================================
# Tests for Assistants project 3-tier context deletion feature
# =============================================================================


@pytest.mark.anyio
async def test_assistants_3tier_delete_from_global_all_context(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test deleting logs from 'All/Transcripts' also removes from User/All and User/Assistant contexts.

    3-tier context hierarchy:
    - Tier 1: All/Transcripts (global aggregate)
    - Tier 2: JohnDoe/All/Transcripts (user aggregate)
    - Tier 3: JohnDoe/AdaLovelace/Transcripts (user + assistant specific)

    Deleting from 'All/Transcripts' should also remove from both other tiers.
    """
    project_name = "Assistants"
    global_all_context = "All/Transcripts"
    user_all_context = "JohnDoe/All/Transcripts"
    user_assistant_context = "JohnDoe/AdaLovelace/Transcripts"

    # Create the Assistants project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log with both _user and _assistant fields
    response = await _create_log(
        client,
        project_name,
        entries={
            "message": "Hello from Ada",
            "role": "assistant",
            "_user": "JohnDoe",
            "_assistant": "AdaLovelace",
        },
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add the log to other contexts
    for ctx in [user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Verify log is in all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id in [log["id"] for log in response.json()["logs"]]

    # Delete from All/Transcripts - should also remove from both other tiers
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify log is removed from ALL three contexts (3-tier removal)
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_assistants_3tier_delete_from_user_assistant_context(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test deleting logs from 'User/Assistant/Transcripts' also removes from other tiers.

    3-tier context hierarchy:
    - Tier 1: All/Transcripts (global aggregate)
    - Tier 2: JaneDoe/All/Transcripts (user aggregate)
    - Tier 3: JaneDoe/BobSmith/Transcripts (user + assistant specific)

    Deleting from Tier 3 should also remove from Tier 1 and Tier 2.
    """
    project_name = "Assistants"
    global_all_context = "All/Transcripts"
    user_all_context = "JaneDoe/All/Transcripts"
    user_assistant_context = "JaneDoe/BobSmith/Transcripts"

    # Create the Assistants project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log in the user+assistant context with _user and _assistant fields
    response = await _create_log(
        client,
        project_name,
        entries={
            "message": "Hello from Bob",
            "role": "assistant",
            "_user": "JaneDoe",
            "_assistant": "BobSmith",
        },
        context=user_assistant_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add the log to the other contexts
    for ctx in [global_all_context, user_all_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Verify log is in all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id in [log["id"] for log in response.json()["logs"]]

    # Delete from User/Assistant context - should also remove from other tiers
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=user_assistant_context,
    )
    assert response.status_code == 200, response.json()

    # Verify log is removed from ALL three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_assistants_3tier_preserves_unrelated_context(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test that logs in an unrelated context are preserved when deleting from 3-tier contexts.

    If a log exists in:
    - All/Transcripts (Tier 1)
    - JohnDoe/All/Transcripts (Tier 2)
    - JohnDoe/AdaLovelace/Transcripts (Tier 3)
    - Archive/OldLogs (unrelated context)

    Deleting from All/Transcripts should:
    - Remove from All/Transcripts
    - Remove from JohnDoe/All/Transcripts
    - Remove from JohnDoe/AdaLovelace/Transcripts
    - PRESERVE in Archive/OldLogs
    """
    project_name = "Assistants"
    global_all_context = "All/Transcripts"
    user_all_context = "JohnDoe/All/Transcripts"
    user_assistant_context = "JohnDoe/AdaLovelace/Transcripts"
    archive_context = "Archive/OldLogs"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all four contexts
    for ctx in [
        global_all_context,
        user_all_context,
        user_assistant_context,
        archive_context,
    ]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log with _user and _assistant fields
    response = await _create_log(
        client,
        project_name,
        entries={
            "message": "Archived message",
            "role": "assistant",
            "_user": "JohnDoe",
            "_assistant": "AdaLovelace",
        },
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to all other contexts
    for ctx in [user_all_context, user_assistant_context, archive_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Delete from All/Transcripts - should cascade to related 3-tier contexts
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()

    # Verify log is removed from all 3-tier contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [log["id"] for log in response.json()["logs"]]

    # Verify log is PRESERVED in Archive/OldLogs (unrelated context)
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"context": archive_context},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_assistants_3tier_partial_siblings_exist(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test deletion when only some sibling contexts exist.

    If a log exists only in 'All/Transcripts' but the corresponding
    User/All and User/Assistant contexts don't exist, the deletion should proceed normally.
    """
    project_name = "Assistants"
    global_all_context = "All/Transcripts"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create only the All/Transcripts context (no other tier contexts)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": global_all_context},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create a log with _user and _assistant fields pointing to non-existent contexts
    response = await _create_log(
        client,
        project_name,
        entries={
            "message": "Orphan log",
            "role": "assistant",
            "_user": "NonExistentUser",
            "_assistant": "NonExistentAssistant",
        },
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Delete from All/Transcripts - should work without sibling contexts existing
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify log is removed
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"context": global_all_context},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_assistants_3tier_delete_from_user_all_context(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test deletion from User/All context cascades correctly.

    3-tier context hierarchy:
    - Tier 1: All/Transcripts (global aggregate)
    - Tier 2: JohnDoe/All/Transcripts (user aggregate)
    - Tier 3: JohnDoe/AdaLovelace/Transcripts (user + assistant specific)

    Deleting from Tier 2 (User/All) should cascade to Tier 1 and Tier 3.
    """
    project_name = "Assistants"
    global_all_context = "All/Transcripts"
    user_all_context = "JohnDoe/All/Transcripts"
    user_assistant_context = "JohnDoe/AdaLovelace/Transcripts"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create log with _user and _assistant fields
    response = await _create_log(
        client,
        project_name,
        entries={
            "message": "Shared message",
            "role": "system",
            "_user": "JohnDoe",
            "_assistant": "AdaLovelace",
        },
        context=user_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to other contexts
    for ctx in [global_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Verify log exists in all contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id in [log["id"] for log in response.json()["logs"]]

    # Delete from User/All context - should cascade to both other tiers
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=user_all_context,
    )
    assert response.status_code == 200, response.json()

    # Verify removed from ALL three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_non_assistants_project_normal_behavior(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test that non-Assistants projects don't have 3-tier context deletion.

    For a regular project with similar context naming, deleting from one
    context should NOT automatically delete from the other.
    """
    project_name = "MyRegularProject"
    all_context = "All/Data"
    specific_context = "Experiment1/Data"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create both contexts
    for ctx in [all_context, specific_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log in all context
    response = await _create_log(
        client,
        project_name,
        entries={"value": 42, "label": "test"},
        context=all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to specific context
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": specific_context, "log_ids": [log_id]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Delete from all context
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=all_context,
    )
    assert response.status_code == 200, response.json()

    # Verify log is removed from all_context
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"context": all_context},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id not in [log["id"] for log in response.json()["logs"]]

    # Verify log is STILL in specific_context (no dual-context behavior)
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"context": specific_context},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_assistants_context_without_slash_normal_behavior(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test that Assistants project contexts without '/' don't trigger 3-tier deletion.

    If context name is just 'Transcripts' (no '/'), the 3-tier context logic
    should not apply.
    """
    project_name = "Assistants"
    simple_context = "Transcripts"
    another_context = "Archive"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create both contexts (neither has '/')
    for ctx in [simple_context, another_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log
    response = await _create_log(
        client,
        project_name,
        entries={"message": "Simple context test", "role": "user"},
        context=simple_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to another context
    response = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": another_context, "log_ids": [log_id]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Delete from simple_context
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=simple_context,
    )
    assert response.status_code == 200, response.json()

    # Verify log is removed from simple_context
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"context": simple_context},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id not in [log["id"] for log in response.json()["logs"]]

    # Verify log is STILL in another_context (no dual-context for simple names)
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"context": another_context},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert log_id in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_assistants_3tier_delete_fields_preserves_system_fields(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test that deleting user fields from Assistants logs preserves _user and _assistant fields.

    Since _user and _assistant are system fields that won't be deleted in practice,
    logs in the Assistants project will never become truly empty. This test verifies that:
    1. Deleting user fields works correctly
    2. The _user and _assistant fields remain intact
    3. The log is not considered empty and stays in all contexts
    """
    project_name = "Assistants"
    global_all_context = "All/Transcripts"
    user_all_context = "CharlieDoe/All/Transcripts"
    user_assistant_context = "CharlieDoe/AdaLovelace/Transcripts"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log with a user field + _user + _assistant
    response = await _create_log(
        client,
        project_name,
        entries={
            "single_field": "value",
            "_user": "CharlieDoe",
            "_assistant": "AdaLovelace",
        },
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to other contexts
    for ctx in [user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Delete ONLY the user field (not _user/_assistant - they're system fields)
    ids_and_fields = [(log_id, ["single_field"])]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=global_all_context,
        delete_empty_logs=True,
    )
    assert response.status_code == 200, response.json()

    # Verify log STILL EXISTS in all contexts (not empty because _user/_assistant remain)
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        logs = response.json()["logs"]
        assert log_id in [log["id"] for log in logs], f"Log should still exist in {ctx}"

        # Verify _user and _assistant fields still exist and single_field was deleted
        log = next(l for l in logs if l["id"] == log_id)
        assert "_user" in log["entries"], "_user field should be preserved"
        assert "_assistant" in log["entries"], "_assistant field should be preserved"
        assert "single_field" not in log["entries"], "single_field should be deleted"


@pytest.mark.anyio
async def test_assistants_3tier_nested_subcontext(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test 3-tier context deletion with nested subcontexts.

    For contexts like:
    - 'All/Calls/Transcripts'
    - 'JohnDoe/All/Calls/Transcripts'
    - 'JohnDoe/AdaLovelace/Calls/Transcripts'

    The 3-tier logic should still work correctly with nested paths.
    """
    project_name = "Assistants"
    global_all_context = "All/Calls/Transcripts"
    user_all_context = "JohnDoe/All/Calls/Transcripts"
    user_assistant_context = "JohnDoe/AdaLovelace/Calls/Transcripts"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all three contexts with nested paths
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log with _user and _assistant fields for nested path
    response = await _create_log(
        client,
        project_name,
        entries={
            "transcript": "Hello, how can I help?",
            "call_id": "123",
            "_user": "JohnDoe",
            "_assistant": "AdaLovelace",
        },
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to other contexts
    for ctx in [user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Delete from All context - uses _user and _assistant fields to find siblings
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()

    # Verify removed from all three nested contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_unitytests_3tier_delete_from_global_all_context(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test that UnityTests project uses the same 3-tier context deletion as Assistants.

    3-tier context hierarchy:
    - Tier 1: All/Transcripts (global aggregate)
    - Tier 2: JohnDoe/All/Transcripts (user aggregate)
    - Tier 3: JohnDoe/AdaLovelace/Transcripts (user + assistant specific)

    Deleting from 'All/Transcripts' should also remove from both other tiers.
    """
    project_name = "UnityTests-MyProject"
    global_all_context = "All/Transcripts"
    user_all_context = "JohnDoe/All/Transcripts"
    user_assistant_context = "JohnDoe/AdaLovelace/Transcripts"

    # Create the project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log with both _user and _assistant fields
    response = await _create_log(
        client,
        project_name,
        entries={
            "message": "Hello from Ada",
            "role": "assistant",
            "_user": "JohnDoe",
            "_assistant": "AdaLovelace",
        },
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add the log to other contexts
    for ctx in [user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Verify log is in all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id in [log["id"] for log in response.json()["logs"]]

    # Delete from All/Transcripts - should also remove from both other tiers
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify log is removed from ALL three contexts (3-tier removal)
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [log["id"] for log in response.json()["logs"]]


@pytest.mark.anyio
async def test_assistants_3tier_with_prefix(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test 3-tier context deletion with arbitrary prefix before the hierarchy.

    This tests contexts like Unity's test isolation paths:
    - Tier 1: tests/test_foo/All/Contacts (global aggregate with prefix)
    - Tier 2: tests/test_foo/DefaultUser/All/Contacts (user aggregate with prefix)
    - Tier 3: tests/test_foo/DefaultUser/Assistant/Contacts (user + assistant with prefix)

    The prefix can have arbitrary depth. The 3-tier logic should detect the hierarchy
    by finding the Tier 1 context (shortest context containing 'All') and parsing it
    to extract prefix and SubContext dynamically.
    """
    project_name = "UnityTests-PrefixTest"
    # Prefix simulates Unity's test isolation paths
    prefix = "tests/test_contact_manager/test_foo"
    global_all_context = f"{prefix}/All/Contacts"
    user_all_context = f"{prefix}/DefaultUser/All/Contacts"
    user_assistant_context = f"{prefix}/DefaultUser/Assistant/Contacts"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all three contexts with prefix
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log with _user and _assistant fields
    response = await _create_log(
        client,
        project_name,
        entries={
            "name": "John Doe",
            "email": "john@example.com",
            "_user": "DefaultUser",
            "_assistant": "Assistant",
        },
        context=user_assistant_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to other contexts (simulating Unity's _add_to_all behavior)
    for ctx in [global_all_context, user_all_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Verify log is in all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id in [
            log["id"] for log in response.json()["logs"]
        ], f"Log should be in {ctx}"

    # Delete from Tier 3 (user+assistant context) - should cascade to other tiers
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=user_assistant_context,
    )
    assert response.status_code == 200, response.json()

    # Verify log is removed from ALL three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [
            log["id"] for log in response.json()["logs"]
        ], f"Log should be removed from {ctx}"


@pytest.mark.anyio
async def test_assistants_3tier_with_prefix_and_nested_subcontext(
    client: AsyncClient,
    use_jsonb_mode,
):
    """
    Test 3-tier deletion with both prefix AND nested subcontext.

    This is the most complex case:
    - Tier 1: tests/test_foo/All/Functions/Compositional
    - Tier 2: tests/test_foo/DefaultUser/All/Functions/Compositional
    - Tier 3: tests/test_foo/DefaultUser/Assistant/Functions/Compositional

    Both prefix and SubContext can have arbitrary depth.
    """
    project_name = "UnityTests-ComplexPath"
    prefix = "tests/test_function_manager/test_bar"
    sub_context = "Functions/Compositional"
    global_all_context = f"{prefix}/All/{sub_context}"
    user_all_context = f"{prefix}/DefaultUser/All/{sub_context}"
    user_assistant_context = f"{prefix}/DefaultUser/Assistant/{sub_context}"

    # Create project
    response = await _create_project(client, project_name)
    assert response.status_code == 200, response.json()

    # Create all three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Create a log in Tier 1 context
    response = await _create_log(
        client,
        project_name,
        entries={
            "function_name": "compose_greet",
            "code": "def compose_greet(): pass",
            "_user": "DefaultUser",
            "_assistant": "Assistant",
        },
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Add to other contexts
    for ctx in [user_all_context, user_assistant_context]:
        response = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Delete from Tier 1 (global All context) - should cascade to other tiers
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        context=global_all_context,
    )
    assert response.status_code == 200, response.json()

    # Verify log is removed from ALL three contexts
    for ctx in [global_all_context, user_all_context, user_assistant_context]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={"context": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert log_id not in [
            log["id"] for log in response.json()["logs"]
        ], f"Log should be removed from {ctx}"


# =============================================================================
# JSONB Mode-Specific Tests
# =============================================================================


@pytest.mark.anyio
async def test_delete_logs_source_type_derived_rejected_in_jsonb(
    client: AsyncClient,
    use_jsonb_mode,
):
    """Test that source_type='derived' returns different errors in JSONB vs EAV mode.

    Both modes reject source_type='derived' when deleting without specifying fields,
    but the error messages differ:
    - JSONB: "JSONB mode does not distinguish" (source_type not supported)
    - EAV: "Cannot delete derived logs without specifying fields" (operation not allowed)
    """
    project_name = "source-type-derived-test"
    _ = await _create_project(client, project_name)

    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Try to delete with source_type='derived' - both modes return 400 but with different errors
    ids_and_fields = [([log_id], None)]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        source_type="derived",
    )

    assert response.status_code == 400, response.json()
    detail = response.json()["detail"]
    assert_mode_specific(
        eav_condition="Cannot delete derived logs without specifying fields" in detail,
        jsonb_condition="JSONB mode does not distinguish" in detail,
        message="source_type='derived' rejection messages differ by mode",
    )
