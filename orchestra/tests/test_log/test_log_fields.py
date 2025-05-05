import pytest
from httpx import AsyncClient

from . import HEADERS, _create_derived_entry, _create_log, _create_project


@pytest.mark.anyio
async def test_get_fields_with_derived_entries(client: AsyncClient):
    project_name = "test_project_derived"
    _ = await _create_project(client, project_name)

    # Create base logs
    response = await _create_log(
        client,
        project_name,
        params={"param1": "test"},
        entries={"base_field": 100, "temperature": 25.5},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived entries
    derived_configs = [
        {
            "key": "temp_plus_10",
            "equation": "{t:temperature} + 10",
            "referenced_logs": {"t": [log_id]},
        },
        {
            "key": "double_base",
            "equation": "{b:base_field} * 2",
            "referenced_logs": {"b": [log_id]},
        },
    ]

    for config in derived_configs:
        response = await _create_derived_entry(
            client,
            project_name,
            config["key"],
            config["equation"],
            config["referenced_logs"],
        )
        assert response.status_code == 200

    # Get field types and verify response
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()

    # Verify base entries
    assert fields["base_field"]["field_type"] == "entry"
    assert fields["base_field"]["data_type"] == "int"
    assert fields["base_field"]["artifacts"] == ""
    assert fields["base_field"]["created_at"] is not None
    assert fields["base_field"]["mutable"] is True

    assert fields["temperature"]["field_type"] == "entry"
    assert fields["temperature"]["data_type"] == "float"
    assert fields["temperature"]["artifacts"] == ""
    assert fields["temperature"]["created_at"] is not None
    assert fields["temperature"]["mutable"] is True

    # Verify params
    assert fields["param1"]["field_type"] == "param"
    assert fields["param1"]["data_type"] == "str"
    assert fields["param1"]["artifacts"] == ""
    assert fields["param1"]["created_at"] is not None
    assert fields["param1"]["mutable"] is True

    # Verify derived entries
    assert fields["temp_plus_10"]["field_type"] == "derived_entry"
    assert fields["temp_plus_10"]["data_type"] == "float"
    assert fields["temp_plus_10"]["artifacts"] == "{t:temperature} + 10"
    assert fields["temp_plus_10"]["created_at"] is not None
    assert (
        fields["temp_plus_10"]["mutable"] is False
    )  # Derived entries are always immutable

    assert fields["double_base"]["field_type"] == "derived_entry"
    assert fields["double_base"]["data_type"] == "int"
    assert fields["double_base"]["artifacts"] == "{b:base_field} * 2"
    assert fields["double_base"]["created_at"] is not None
    assert (
        fields["double_base"]["mutable"] is False
    )  # Derived entries are always immutable

    # Verify field ordering by created_at
    created_times = [fields[k]["created_at"] for k in fields.keys()]
    assert created_times == sorted(created_times)


@pytest.mark.anyio
async def test_rename_field_basic(client: AsyncClient):
    """Test basic field renaming functionality."""
    project_name = "test-rename-field"
    _ = await _create_project(client, project_name)

    # Create initial logs with old field name
    initial_entries = {
        "old_field_name": "test value",
        "other_field": 42,
        "explicit_types": {
            "old_field_name": {"type": "str", "mutable": True},
            "other_field": {"type": "int", "mutable": True},
        },
    }
    response = await _create_log(client, project_name, entries=initial_entries)
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Rename the field
    rename_response = await client.post(
        "/v0/logs/rename_field",
        json={
            "project": project_name,
            "old_field_name": "old_field_name",
            "new_field_name": "new_field_name",
        },
        headers=HEADERS,
    )
    assert rename_response.status_code == 200, rename_response.json()

    # Verify field types are updated
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200, field_types_response.json()
    field_types = field_types_response.json()

    # Check old field is gone and new field exists with same type info
    assert "old_field_name" not in field_types
    assert "new_field_name" in field_types
    assert field_types["new_field_name"]["data_type"] == "str"
    assert field_types["new_field_name"]["mutable"] is True

    # Verify logs are updated
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200, logs_response.json()
    logs = logs_response.json()["logs"]

    # Check log entries use new field name
    assert len(logs) == 1
    log = logs[0]
    assert "old_field_name" not in log["entries"]
    assert "new_field_name" in log["entries"]
    assert log["entries"]["new_field_name"] == "test value"


@pytest.mark.anyio
async def test_rename_field_edge_cases(client: AsyncClient):
    """Test edge cases for field renaming functionality."""
    project_name = "test-rename-field-edges"
    _ = await _create_project(client, project_name)

    # Create a log with existing fields
    initial_entries = {
        "existing_field": "test value",
        "other_field": "other value",
        "explicit_types": {
            "existing_field": {"type": "str", "mutable": True},
            "other_field": {"type": "str", "mutable": True},
        },
    }
    response = await _create_log(client, project_name, entries=initial_entries)
    assert response.status_code == 200

    # Test case 1: Attempt to rename non-existent field
    response = await client.post(
        "/v0/logs/rename_field",
        json={
            "project": project_name,
            "old_field_name": "nonexistent_field",
            "new_field_name": "new_field",
        },
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert "Field not found" in response.json()["detail"]

    # Test case 2: Attempt to rename to an existing field name
    response = await client.post(
        "/v0/logs/rename_field",
        json={
            "project": project_name,
            "old_field_name": "existing_field",
            "new_field_name": "other_field",
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]

    # Test case 3: Attempt to rename with invalid new field name
    response = await client.post(
        "/v0/logs/rename_field",
        json={
            "project": project_name,
            "old_field_name": "existing_field",
            "new_field_name": "",  # Empty string
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Invalid field name" in response.json()["detail"]


@pytest.mark.anyio
async def test_field_type_constraints_and_mutability(client: AsyncClient):
    """Test that fields maintain their type (entry/param/derived) consistently and respect mutability."""
    project_name = "test_field_type_constraints"
    await _create_project(client, project_name)

    # Create a parameter
    param_response = await _create_log(
        client,
        project_name,
        params={"test_field": "value"},
        entries={},
    )
    assert param_response.status_code == 200, param_response.json()

    # Try to create an entry with the same name (should fail)
    entry_response = await _create_log(
        client,
        project_name,
        entries={"test_field": "value"},
        params={},
    )
    assert entry_response.status_code == 400, entry_response.json()
    assert "already exists as a param" in entry_response.json()["detail"]
    assert "Cannot create it as an entry" in entry_response.json()["detail"]

    # Verify field type and mutability in field types
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["test_field"]["field_type"] == "param"
    assert field_types["test_field"]["mutable"] is True
    assert field_types["test_field"]["created_at"] is not None

    # Create an entry
    entry_response = await _create_log(
        client,
        project_name,
        entries={"entry_field": "value"},
        params={},
    )
    assert entry_response.status_code == 200, entry_response.json()

    # Try to create a parameter with the same name (should fail)
    param_response = await _create_log(
        client,
        project_name,
        params={"entry_field": "value"},
        entries={},
    )
    assert param_response.status_code == 400, param_response.json()
    assert "already exists as an entry" in param_response.json()["detail"]
    assert "Cannot create it as a param" in param_response.json()["detail"]

    # Create a derived entry
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key="derived_field",
        equation="{x:entry_field}",
        referenced_logs={"x": [3]},
    )
    assert derived_response.status_code == 200, derived_response.json()

    # Try to create an entry with the same name as derived (should fail)
    entry_response = await _create_log(
        client,
        project_name,
        entries={"derived_field": "value"},
        params={},
    )
    assert entry_response.status_code == 400, entry_response.json()
    assert "already exists as a derived_entry" in entry_response.json()["detail"]
    assert "Cannot create it as an entry" in entry_response.json()["detail"]

    # Try to create a param with the same name as derived (should fail)
    param_response = await _create_log(
        client,
        project_name,
        params={"derived_field": "value"},
        entries={},
    )
    assert param_response.status_code == 400, param_response.json()
    assert "already exists as a derived_entry" in param_response.json()["detail"]
    assert "Cannot create it as a param" in param_response.json()["detail"]

    # Try to create a derived entry with same name as param (should fail)
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key="test_field",  # This is already a param
        equation="{x:entry_field}",
        referenced_logs={"x": [3]},
    )
    assert derived_response.status_code == 500, derived_response.json()
    assert "already exists as a param" in derived_response.json()["detail"]
    assert "Cannot create it as a derived_entry" in derived_response.json()["detail"]

    # Try to create a derived entry with same name as entry (should fail)
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key="entry_field",  # This is already an entry
        equation="{x:entry_field}",
        referenced_logs={"x": [3]},
    )
    assert derived_response.status_code == 500, derived_response.json()
    assert "already exists as an entry" in derived_response.json()["detail"]
    assert "Cannot create it as a derived_entry" in derived_response.json()["detail"]


@pytest.mark.anyio
async def test_rename_field_preserves_order(client: AsyncClient):
    """Test that renaming a field preserves the original field order."""
    project_name = "test-rename-field-order"
    _ = await _create_project(client, project_name)

    # Create a log with fields in a specific order
    initial_entries = {
        "field_a": "value a",
        "field_b": "value b",
        "field_c": "value c",
        "explicit_types": {
            "field_a": {"type": "str", "mutable": True},
            "field_b": {"type": "str", "mutable": True},
            "field_c": {"type": "str", "mutable": True},
        },
    }
    response = await _create_log(client, project_name, entries=initial_entries)
    assert response.status_code == 200

    # Get initial field order
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    initial_order = list(fields_response.json().keys())

    # Find the index of field_b
    field_b_index = initial_order.index("field_b")

    # Get initial log entries and their order
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    initial_log_order = list(logs[0]["entries"].keys())
    # Find the index of field_b in the log entries
    log_field_b_index = initial_log_order.index("field_b")

    # Rename field_b to field_b_renamed
    rename_response = await client.post(
        "/v0/logs/rename_field",
        json={
            "project": project_name,
            "old_field_name": "field_b",
            "new_field_name": "field_b_renamed",
        },
        headers=HEADERS,
    )
    assert rename_response.status_code == 200, rename_response.json()

    # Get new field order after renaming
    fields_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    new_order = list(fields_response.json().keys())

    # Verify field_b is removed and field_b_renamed appears at the same index
    assert "field_b" not in new_order
    assert "field_b_renamed" in new_order
    assert new_order.index("field_b_renamed") == field_b_index

    # Verify the overall order is preserved with only the name change
    expected_order = initial_order.copy()
    expected_order[field_b_index] = "field_b_renamed"
    assert new_order == expected_order

    # Get logs after renaming and verify entry order is preserved
    logs_response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    new_log_order = list(logs[0]["entries"].keys())

    # Verify field_b is removed and field_b_renamed appears at the same index in log entries
    assert "field_b" not in new_log_order
    assert "field_b_renamed" in new_log_order
    assert new_log_order.index("field_b_renamed") == log_field_b_index

    # Verify the overall log entry order is preserved with only the name change
    expected_log_order = initial_log_order.copy()
    expected_log_order[log_field_b_index] = "field_b_renamed"
    assert new_log_order == expected_log_order
