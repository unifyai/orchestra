import pytest
from httpx import AsyncClient

from . import HEADERS, _create_derived_entry, _create_log, _create_project


@pytest.mark.anyio
async def test_get_fields_with_derived_entries(client: AsyncClient):
    project_name = "test_project_derived"
    _ = await _create_project(client, project_name)

    # Implicit creation for entries - types inferred from values
    response = await _create_log(
        client,
        project_name,
        entries={
            "base_field_implicit": 100,
            "temperature_implicit": 25.5,
            "param1_implicit": "test",
        },
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Implicit fields have types inferred from values
    resp_fields_implicit = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert resp_fields_implicit.status_code == 200
    f_implicit = resp_fields_implicit.json()
    assert (
        f_implicit["param1_implicit"]["field_type"] == "entry"
    )  # All fields are entries now
    assert (
        f_implicit["param1_implicit"]["data_type"] == "str"
    )  # Type inferred from value
    assert f_implicit["base_field_implicit"]["field_type"] == "entry"
    assert (
        f_implicit["base_field_implicit"]["data_type"] == "int"
    )  # Type inferred from value
    assert f_implicit["temperature_implicit"]["field_type"] == "entry"
    assert (
        f_implicit["temperature_implicit"]["data_type"] == "float"
    )  # Type inferred from value

    # Create derived entries
    derived_configs = [
        {
            "key": "temp_plus_10",
            "equation": "{t:temperature_implicit} + 10",
            "referenced_logs": {"t": [log_id]},
        },
        {
            "key": "double_base",
            "equation": "{b:base_field_implicit} * 2",
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

    # Get field types and verify response after implicit creation
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()

    # Verify base entries (implicit fields infer types from values)
    assert fields["base_field_implicit"]["field_type"] == "entry"
    assert (
        fields["base_field_implicit"]["data_type"] == "int"
    )  # Type inferred from value
    assert fields["base_field_implicit"]["artifacts"] == ""
    assert fields["base_field_implicit"]["created_at"] is not None
    assert fields["base_field_implicit"]["mutable"] is True

    assert fields["temperature_implicit"]["field_type"] == "entry"
    assert (
        fields["temperature_implicit"]["data_type"] == "float"
    )  # Type inferred from value
    assert fields["temperature_implicit"]["artifacts"] == ""
    assert fields["temperature_implicit"]["created_at"] is not None
    assert fields["temperature_implicit"]["mutable"] is True

    # Verify implicit fields infer types from values
    assert (
        fields["param1_implicit"]["field_type"] == "entry"
    )  # All fields are entries now
    assert fields["param1_implicit"]["data_type"] == "str"  # Type inferred from value
    assert fields["param1_implicit"]["artifacts"] == ""
    assert fields["param1_implicit"]["created_at"] is not None
    assert fields["param1_implicit"]["mutable"] is True

    # Verify derived entries
    assert fields["temp_plus_10"]["field_type"] == "derived_entry"
    assert fields["temp_plus_10"]["data_type"] == "float"
    assert fields["temp_plus_10"]["artifacts"] == "{t:temperature_implicit} + 10"
    assert fields["temp_plus_10"]["created_at"] is not None
    assert (
        fields["temp_plus_10"]["mutable"] is False
    )  # Derived entries are always immutable

    assert fields["double_base"]["field_type"] == "derived_entry"
    assert fields["double_base"]["data_type"] == "int"
    assert fields["double_base"]["artifacts"] == "{b:base_field_implicit} * 2"
    assert fields["double_base"]["created_at"] is not None
    assert (
        fields["double_base"]["mutable"] is False
    )  # Derived entries are always immutable

    # Verify field ordering by created_at
    created_times = [fields[k]["created_at"] for k in fields.keys()]
    assert created_times == sorted(created_times)

    # Explicit creation with new names - data_type matches explicit types
    response_explicit = await _create_log(
        client,
        project_name,
        entries={
            "base_field": 100,
            "temperature": 25.5,
            "explicit_types": {
                "base_field": {"type": "int", "mutable": True},
                "temperature": {"type": "float", "mutable": True},
            },
        },
    )
    assert response_explicit.status_code == 200

    fields_after = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_after.status_code == 200
    fields2 = fields_after.json()
    assert fields2["base_field"]["data_type"] == "int"
    assert fields2["temperature"]["data_type"] == "float"

    # Explicit derived entry creation based on explicit base fields
    log_id2 = response_explicit.json()["log_event_ids"][0]
    derived_explicit_configs = [
        {
            "key": "temp_plus_10_explicit",
            "equation": "{t:temperature} + 10",
            "referenced_logs": {"t": [log_id2]},
        },
        {
            "key": "double_base_explicit",
            "equation": "{b:base_field} * 2",
            "referenced_logs": {"b": [log_id2]},
        },
    ]

    for config in derived_explicit_configs:
        resp = await _create_derived_entry(
            client,
            project_name,
            config["key"],
            config["equation"],
            config["referenced_logs"],
        )
        assert resp.status_code == 200

    # Verify explicit derived entries in get_fields
    fields_resp_exp = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_resp_exp.status_code == 200
    fields_exp = fields_resp_exp.json()

    assert fields_exp["temp_plus_10_explicit"]["field_type"] == "derived_entry"
    assert fields_exp["temp_plus_10_explicit"]["data_type"] == "float"
    assert fields_exp["temp_plus_10_explicit"]["artifacts"] == "{t:temperature} + 10"
    assert fields_exp["temp_plus_10_explicit"]["created_at"] is not None
    assert fields_exp["temp_plus_10_explicit"]["mutable"] is False

    assert fields_exp["double_base_explicit"]["field_type"] == "derived_entry"
    assert fields_exp["double_base_explicit"]["data_type"] == "int"
    assert fields_exp["double_base_explicit"]["artifacts"] == "{b:base_field} * 2"
    assert fields_exp["double_base_explicit"]["created_at"] is not None
    assert fields_exp["double_base_explicit"]["mutable"] is False


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
    rename_response = await client.patch(
        "/v0/logs/rename_field",
        json={
            "project_name": project_name,
            "old_field_name": "old_field_name",
            "new_field_name": "new_field_name",
        },
        headers=HEADERS,
    )
    assert rename_response.status_code == 200, rename_response.json()

    # Verify field types are updated
    field_types_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
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
        f"/v0/logs?project_name={project_name}",
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
    response = await client.patch(
        "/v0/logs/rename_field",
        json={
            "project_name": project_name,
            "old_field_name": "nonexistent_field",
            "new_field_name": "new_field",
        },
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert "Field not found" in response.json()["detail"]

    # Test case 2: Attempt to rename to an existing field name
    response = await client.patch(
        "/v0/logs/rename_field",
        json={
            "project_name": project_name,
            "old_field_name": "existing_field",
            "new_field_name": "other_field",
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]

    # Test case 3: Attempt to rename with invalid new field name
    response = await client.patch(
        "/v0/logs/rename_field",
        json={
            "project_name": project_name,
            "old_field_name": "existing_field",
            "new_field_name": "",  # Empty string
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Invalid field name" in response.json()["detail"]


@pytest.mark.anyio
async def test_field_type_constraints_and_mutability(
    client: AsyncClient,
):
    """Test that fields maintain their type (entry/derived) consistently and respect mutability."""
    project_name = "test_field_type_constraints"
    await _create_project(client, project_name)

    # Create an entry field
    entry_response = await _create_log(
        client,
        project_name,
        entries={"test_field": "value"},
    )
    assert entry_response.status_code == 200, entry_response.json()

    # Verify field type and mutability in field types
    field_types_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["test_field"]["field_type"] == "entry"
    assert field_types["test_field"]["mutable"] is True
    assert field_types["test_field"]["created_at"] is not None

    # Create another entry
    entry_response = await _create_log(
        client,
        project_name,
        entries={"entry_field": "value"},
    )
    assert entry_response.status_code == 200, entry_response.json()

    # Creating another log with the same field name is allowed (it's the same entry)
    entry_response = await _create_log(
        client,
        project_name,
        entries={"entry_field": "another_value"},
    )
    assert entry_response.status_code == 200, entry_response.json()

    # Create a derived entry
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key="derived_field",
        equation="{x:entry_field}",
        referenced_logs={"x": [2]},
    )
    assert derived_response.status_code == 200, derived_response.json()

    # Try to create an entry with the same name as derived (should fail)
    entry_response = await _create_log(
        client,
        project_name,
        entries={"derived_field": "value"},
    )
    assert entry_response.status_code == 400, entry_response.json()
    assert "already exists as a derived_entry" in entry_response.json()["detail"]
    assert "Cannot create it as an entry" in entry_response.json()["detail"]

    # Try to create a derived entry with same name as entry (should fail)
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key="test_field",  # This is already an entry
        equation="{x:entry_field}",
        referenced_logs={"x": [2]},  # Use log 2 which has entry_field
    )
    assert derived_response.status_code == 500, derived_response.json()
    assert "already exists as an entry" in derived_response.json()["detail"]
    assert "Cannot create it as a derived_entry" in derived_response.json()["detail"]

    # Try to create a derived entry with same name as another entry (should fail)
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key="entry_field",  # This is already an entry
        equation="{x:entry_field}",
        referenced_logs={"x": [2]},
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
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    initial_order = list(fields_response.json().keys())

    # Find the index of field_b
    field_b_index = initial_order.index("field_b")

    # Get initial log entries and their order
    logs_response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 1
    initial_log_order = list(logs[0]["entries"].keys())
    # Find the index of field_b in the log entries
    log_field_b_index = initial_log_order.index("field_b")

    # Rename field_b to field_b_renamed
    rename_response = await client.patch(
        "/v0/logs/rename_field",
        json={
            "project_name": project_name,
            "old_field_name": "field_b",
            "new_field_name": "field_b_renamed",
        },
        headers=HEADERS,
    )
    assert rename_response.status_code == 200, rename_response.json()

    # Get new field order after renaming
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
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
        f"/v0/logs?project_name={project_name}",
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


@pytest.mark.anyio
async def test_create_fields_happy_path(client: AsyncClient):
    """Test creating fields with explicit and untyped fields"""
    project_name = "test-create-fields"
    context_name = "fields-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Test context for columns"},
        headers=HEADERS,
    )

    # Create fields with explicit and untyped fields
    fields_data = {
        "project_name": project_name,
        "context": context_name,
        "fields": {
            "accuracy": "float",  # Explicit type
            "value": None,  # Untyped field (defaults to "Any")
        },
    }
    response = await client.post(
        f"/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "Fields created successfully" in response.json()["info"]

    # Verify the fields were created with correct types
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()

    # Check that both fields exist
    assert "accuracy" in fields
    assert "value" in fields

    # Check that the explicit type was set correctly
    assert fields["accuracy"]["data_type"] == "float"

    # Check that the untyped field defaults to "Any"
    assert fields["value"]["data_type"] == "Any"


@pytest.mark.anyio
async def test_create_fields_invalid_type(client: AsyncClient):
    """Test creating fields with an invalid type"""
    project_name = "test-invalid-field-type"
    context_name = "invalid-type-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Test context for invalid field type",
        },
        headers=HEADERS,
    )

    # Try to create a field with an invalid type
    fields_data = {
        "project_name": project_name,
        "context": context_name,
        "fields": {"badfield": "foobar"},  # Invalid type (not a recognized type)
    }
    response = await client.post(
        f"/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Invalid field type" in response.json()["detail"]


@pytest.mark.anyio
async def test_create_fields_json_schema_types(client: AsyncClient):
    """Test creating fields with standard JSON Schema type names.

    Orchestra now accepts standard JSON Schema types alongside Python-style types:
    - "string" -> "str"
    - "integer" -> "int"
    - "number" -> "float"
    - "boolean" -> "bool"
    - "array" -> "list"
    - "object" -> "dict"
    """
    project_name = "test-json-schema-types"
    context_name = "json-schema-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Test context for JSON Schema types",
        },
        headers=HEADERS,
    )

    # Create fields using JSON Schema type names (simple strings)
    fields_data = {
        "project_name": project_name,
        "context": context_name,
        "fields": {
            "string_field": "string",
            "integer_field": "integer",
            "number_field": "number",
            "boolean_field": "boolean",
            "array_field": "array",
            "object_field": "object",
        },
    }
    response = await client.post(
        "/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify the fields were created with normalized types
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()

    # JSON Schema types should be normalized to Orchestra internal types
    assert fields["string_field"]["data_type"] == "str"
    assert fields["integer_field"]["data_type"] == "int"
    assert fields["number_field"]["data_type"] == "float"
    assert fields["boolean_field"]["data_type"] == "bool"
    assert fields["array_field"]["data_type"] == "list"
    assert fields["object_field"]["data_type"] == "dict"


@pytest.mark.anyio
async def test_create_fields_json_schema_with_constraints(client: AsyncClient):
    """Test creating fields with full JSON Schema definitions including constraints.

    This tests the StandardFieldDefinition path where the type field contains
    a JSON Schema type name with additional properties.
    """
    project_name = "test-json-schema-constraints"
    context_name = "constraints-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Test context for JSON Schema with constraints",
        },
        headers=HEADERS,
    )

    # Create fields using JSON Schema type names with constraints
    fields_data = {
        "project_name": project_name,
        "context": context_name,
        "fields": {
            # Standard definition with JSON Schema type
            "name_field": {
                "type": "string",
                "description": "User name",
            },
            # Standard definition with constraints (constraints are stored in schema)
            "age_field": {
                "type": "integer",
                "description": "User age",
            },
            # Standard definition with format
            "timestamp_field": {
                "type": "string",
                "description": "ISO timestamp",
            },
        },
    }
    response = await client.post(
        "/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify the fields were created
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()

    # Types should be normalized
    assert fields["name_field"]["data_type"] == "str"
    assert fields["age_field"]["data_type"] == "int"
    assert fields["timestamp_field"]["data_type"] == "str"


@pytest.mark.anyio
async def test_create_fields_full_json_schema(client: AsyncClient):
    """Test creating fields with full JSON Schema objects (anyOf, $ref, etc.).

    This tests the JsonSchemaFieldDefinition path for complex schemas that
    don't fit the StandardFieldDefinition pattern.
    """
    project_name = "test-full-json-schema"
    context_name = "full-schema-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Test context for full JSON Schema",
        },
        headers=HEADERS,
    )

    # Create fields using full JSON Schema definitions
    fields_data = {
        "project_name": project_name,
        "context": context_name,
        "fields": {
            # anyOf for optional type (common in Pydantic's model_json_schema)
            "optional_field": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Optional string field",
            },
            # array with items
            "list_of_ints": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "List of integers",
            },
            # $ref style (common in Pydantic for nested models)
            "ref_field": {
                "$ref": "#/$defs/SomeModel",
                "description": "Reference to a model",
            },
        },
    }
    response = await client.post(
        "/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify the fields were created with JSON schemas stored
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()

    # Fields should exist - the exact stored type depends on schema handling
    assert "optional_field" in fields
    assert "list_of_ints" in fields
    assert "ref_field" in fields


@pytest.mark.anyio
async def test_create_fields_mixed_types(client: AsyncClient):
    """Test creating fields with a mix of type formats in a single request."""
    project_name = "test-mixed-types"
    context_name = "mixed-context"

    # Setup project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": context_name,
            "description": "Test context for mixed type formats",
        },
        headers=HEADERS,
    )

    # Create fields using a mix of formats
    fields_data = {
        "project_name": project_name,
        "context": context_name,
        "fields": {
            # Python-style simple string
            "python_str": "str",
            # JSON Schema simple string
            "json_string": "string",
            # StandardFieldDefinition with Python type
            "std_python": {"type": "int", "mutable": True},
            # StandardFieldDefinition with JSON Schema type
            "std_json": {"type": "integer", "mutable": False},
            # Full JSON Schema
            "full_schema": {
                "anyOf": [{"type": "number"}, {"type": "null"}],
                "description": "Optional number",
            },
            # Enum type
            "status": {"type": "enum", "values": ["active", "inactive"]},
            # Untyped (None)
            "untyped": None,
        },
    }
    response = await client.post(
        "/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify all fields were created
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context={context_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()

    assert fields["python_str"]["data_type"] == "str"
    assert fields["json_string"]["data_type"] == "str"
    assert fields["std_python"]["data_type"] == "int"
    assert fields["std_json"]["data_type"] == "int"
    assert "full_schema" in fields
    assert fields["status"]["data_type"] == "enum"
    assert fields["untyped"]["data_type"] == "Any"


@pytest.mark.anyio
async def test_delete_fields_endpoint(client: AsyncClient):
    """Test deleting fields using the DELETE /v0/logs/fields endpoint"""
    project_name = "test-delete-fields"

    # Create a project
    await _create_project(client, project_name)

    # Create first log with columns col1, col2, and a keeper field that won't be deleted
    # (Logs require at least one field to be returned)
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "col1": 1,
            "col2": 2,
            "_keeper": "keep1",
            "explicit_types": {
                "col1": {"type": "int", "mutable": True},
                "col2": {"type": "int", "mutable": True},
                "_keeper": {"type": "str", "mutable": True},
            },
        },
    )
    assert response1.status_code == 200

    # Create second log with the same columns
    response2 = await _create_log(
        client,
        project_name,
        entries={
            "col1": 10,
            "col2": 20,
            "_keeper": "keep2",
            "explicit_types": {
                "col1": {"type": "int", "mutable": True},
                "col2": {"type": "int", "mutable": True},
                "_keeper": {"type": "str", "mutable": True},
            },
        },
    )
    assert response2.status_code == 200

    # Verify the fields exist
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()
    assert "col1" in fields
    assert "col2" in fields

    # Delete the columns
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/fields",
        json={"project_name": project_name, "fields": ["col1", "col2"]},
        headers=HEADERS,
    )
    assert delete_response.status_code == 200, delete_response.json()
    assert delete_response.json()["deleted_fields"] == ["col1", "col2"]

    # Verify the columns no longer exist
    fields_response_after = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response_after.status_code == 200
    fields_after = fields_response_after.json()
    assert "col1" not in fields_after
    assert "col2" not in fields_after

    # Verify the columns are removed from all logs
    logs_response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]

    # IMPORTANT: Verify that logs still exist (weren't deleted)
    assert len(logs) == 2, "Deleting fields should not delete the log events themselves"

    # Check each log to ensure the deleted columns are gone but keeper remains
    for log in logs:
        assert "col1" not in log["entries"]
        assert "col2" not in log["entries"]
        assert "_keeper" in log["entries"]  # Keeper field should still exist
        # Verify logs still have their structure
        assert "id" in log
        assert "entries" in log
        assert isinstance(log["entries"], dict)


@pytest.mark.anyio
async def test_delete_fields_preserves_log_events(client: AsyncClient):
    """Test that deleting fields only removes the field data, not the entire log events."""
    project_name = "test-delete-fields-preserve-logs"

    # Create a project
    await _create_project(client, project_name)

    # Create logs with multiple fields
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "field_to_delete": "value1",
            "field_to_keep": "keeper1",
            "another_field": "data1",
            "explicit_types": {
                "field_to_delete": {"type": "str", "mutable": True},
                "field_to_keep": {"type": "str", "mutable": True},
                "another_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert response1.status_code == 200
    log_id1 = response1.json()["log_event_ids"][0]

    response2 = await _create_log(
        client,
        project_name,
        entries={
            "field_to_delete": "value2",
            "field_to_keep": "keeper2",
            "another_field": "data2",
            "explicit_types": {
                "field_to_delete": {"type": "str", "mutable": True},
                "field_to_keep": {"type": "str", "mutable": True},
                "another_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert response2.status_code == 200
    log_id2 = response2.json()["log_event_ids"][0]

    # Verify logs exist and have all fields
    logs_response_before = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response_before.status_code == 200
    logs_before = logs_response_before.json()["logs"]
    assert len(logs_before) == 2

    # Verify all fields exist in both logs
    for log in logs_before:
        assert "field_to_delete" in log["entries"]
        assert "field_to_keep" in log["entries"]
        assert "another_field" in log["entries"]

    # Delete only one field
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/fields",
        json={"project_name": project_name, "fields": ["field_to_delete"]},
        headers=HEADERS,
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted_fields"] == ["field_to_delete"]

    # Verify logs STILL exist but without the deleted field
    logs_response_after = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response_after.status_code == 200
    logs_after = logs_response_after.json()["logs"]

    # CRITICAL: Verify we still have the same number of logs
    assert len(logs_after) == 2, "Log events should not be deleted when deleting fields"

    # Verify the deleted field is gone but other fields remain
    for log in logs_after:
        assert "field_to_delete" not in log["entries"]
        assert "field_to_keep" in log["entries"]
        assert "another_field" in log["entries"]

    # Verify we can still find the specific logs with their remaining data
    # Check that both logs have the correct remaining fields and values
    log1 = next((log for log in logs_after if log["id"] == log_id1), None)
    log2 = next((log for log in logs_after if log["id"] == log_id2), None)

    assert log1 is not None, f"Log with ID {log_id1} should still exist"
    assert log2 is not None, f"Log with ID {log_id2} should still exist"

    # Verify log1 has correct remaining fields
    assert log1["entries"]["field_to_keep"] == "keeper1"
    assert log1["entries"]["another_field"] == "data1"

    # Verify log2 has correct remaining fields
    assert log2["entries"]["field_to_keep"] == "keeper2"
    assert log2["entries"]["another_field"] == "data2"


@pytest.mark.anyio
async def test_delete_all_fields_preserves_empty_log_events(
    client: AsyncClient,
):
    """Test that deleting all fields from logs still preserves the log events as empty.

    Log data is stored directly in LogEvent.data, so empty logs are returned.
    """
    project_name = "test-delete-all-fields"

    # Create a project
    await _create_project(client, project_name)

    # Create logs with only two fields
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "field1": "value1",
            "field2": "value2",
            "explicit_types": {
                "field1": {"type": "str", "mutable": True},
                "field2": {"type": "str", "mutable": True},
            },
        },
    )
    assert response1.status_code == 200
    log_id1 = response1.json()["log_event_ids"][0]

    # Delete both fields
    delete_response = await client.request(
        "DELETE",
        "/v0/logs/fields",
        json={"project_name": project_name, "fields": ["field1", "field2"]},
        headers=HEADERS,
    )
    assert delete_response.status_code == 200
    assert set(delete_response.json()["deleted_fields"]) == {"field1", "field2"}

    # Verify log still exists but with empty entries
    logs_response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]

    # CRITICAL: Verify log event still exists even with no fields
    assert len(logs) == 1, "Log event should exist even when all fields are deleted"
    assert logs[0]["id"] == log_id1
    assert (
        logs[0]["entries"] == {}
    ), "Log should have empty entries after all fields are deleted"
    assert "derived_entries" in logs[0]


@pytest.mark.anyio
async def test_create_fields_with_backfill_default(client: AsyncClient):
    """Test that creating fields with default backfill_logs=True adds None values to existing logs."""
    project_name = "test-backfill-default"

    # Create a project
    await _create_project(client, project_name)

    # Create some logs first
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "existing_field": "value1",
            "explicit_types": {
                "existing_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert response1.status_code == 200

    response2 = await _create_log(
        client,
        project_name,
        entries={
            "existing_field": "value2",
            "explicit_types": {
                "existing_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert response2.status_code == 200

    # Create new fields (backfill_logs defaults to True)
    fields_response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "new_field1": "str",
                "new_field2": "int",
            },
        },
        headers=HEADERS,
    )
    assert fields_response.status_code == 200, fields_response.json()
    assert "backfilled_count" in fields_response.json()
    assert fields_response.json()["backfilled_count"] == 4  # 2 logs × 2 new fields

    # Verify logs now have the new fields with None values
    logs_response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 2

    for log in logs:
        assert "existing_field" in log["entries"]
        assert "new_field1" in log["entries"]
        assert "new_field2" in log["entries"]
        assert log["entries"]["new_field1"] is None
        assert log["entries"]["new_field2"] is None


@pytest.mark.anyio
async def test_create_fields_without_backfill(client: AsyncClient):
    """Test that creating fields with backfill_logs=False does not add to existing logs."""
    project_name = "test-no-backfill"

    # Create a project
    await _create_project(client, project_name)

    # Create some logs first
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "existing_field": "value1",
            "explicit_types": {
                "existing_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert response1.status_code == 200

    response2 = await _create_log(
        client,
        project_name,
        entries={
            "existing_field": "value2",
            "explicit_types": {
                "existing_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert response2.status_code == 200

    # Create new fields with backfill_logs=False
    fields_response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "new_field1": "str",
                "new_field2": "int",
            },
            "backfill_logs": False,
        },
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    assert fields_response.json()["backfilled_count"] == 0

    # Verify logs do NOT have the new fields
    logs_response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 2

    for log in logs:
        assert "existing_field" in log["entries"]
        assert "new_field1" not in log["entries"]
        assert "new_field2" not in log["entries"]

    # But the field types should exist
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()
    assert "new_field1" in fields
    assert "new_field2" in fields
    assert fields["new_field1"]["data_type"] == "str"
    assert fields["new_field2"]["data_type"] == "int"


@pytest.mark.anyio
async def test_create_fields_backfill_with_existing_values(
    client: AsyncClient,
):
    """Test that backfill does not overwrite existing field values."""
    project_name = "test-backfill-no-overwrite"

    # Create a project
    await _create_project(client, project_name)

    # Create a log with one of the fields already present
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "existing_field": "value1",
            "new_field1": "already_exists",
            "explicit_types": {
                "existing_field": {"type": "str", "mutable": True},
                "new_field1": {"type": "str", "mutable": True},
            },
        },
    )
    assert response1.status_code == 200
    log_id1 = response1.json()["log_event_ids"][0]

    # Create another log without the new field
    response2 = await _create_log(
        client,
        project_name,
        entries={
            "existing_field": "value2",
            "explicit_types": {
                "existing_field": {"type": "str", "mutable": True},
            },
        },
    )
    assert response2.status_code == 200
    log_id2 = response2.json()["log_event_ids"][0]

    # Create new fields with backfill
    fields_response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "new_field1": "str",
                "new_field2": "int",
            },
        },
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    # Only 3 entries should be backfilled (not 4) because log1 already has new_field1
    assert fields_response.json()["backfilled_count"] == 3

    # Verify the existing value was not overwritten
    logs_response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]

    log1 = next((log for log in logs if log["id"] == log_id1), None)
    log2 = next((log for log in logs if log["id"] == log_id2), None)

    assert log1 is not None
    assert log2 is not None

    # Log1 should keep its existing value for new_field1
    assert log1["entries"]["new_field1"] == "already_exists"
    assert log1["entries"]["new_field2"] is None

    # Log2 should have None for both new fields
    assert log2["entries"]["new_field1"] is None
    assert log2["entries"]["new_field2"] is None


@pytest.mark.anyio
async def test_create_fields_backfill_empty_context(
    client: AsyncClient,
):
    """Test that backfill works correctly when context has no logs."""
    project_name = "test-backfill-empty"

    # Create a project
    await _create_project(client, project_name)

    # Create fields without any existing logs
    fields_response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "field1": "str",
                "field2": "int",
            },
        },
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    assert fields_response.json()["backfilled_count"] == 0

    # Fields should be created
    fields_list_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_list_response.status_code == 200
    fields = fields_list_response.json()
    assert "field1" in fields
    assert "field2" in fields


@pytest.mark.anyio
async def test_create_fields_backfill_respects_derived_logs(
    client: AsyncClient,
):
    """Test that backfill does not create Log entries for fields that exist as DerivedLog entries."""
    project_name = "test-backfill-derived"

    # Create a project
    await _create_project(client, project_name)

    # Create a log
    response = await _create_log(
        client,
        project_name,
        entries={
            "base_field": 10,
            "explicit_types": {
                "base_field": {"type": "int", "mutable": True},
            },
        },
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create a derived entry with a field name we'll try to backfill
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key="computed_field",
        equation="{x:base_field} * 2",
        referenced_logs={"x": [log_id]},
    )
    assert derived_response.status_code == 200

    # Try to create fields including one that already exists as a derived entry
    fields_response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "computed_field": "int",  # This already exists as derived
                "new_field": "str",  # This is new
            },
        },
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    # Should only backfill 1 entry (new_field), not 2
    assert fields_response.json()["backfilled_count"] == 1

    # Verify the log has the new field but NOT a Log entry for computed_field
    logs_response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 1

    log = logs[0]
    # Regular entries should have base_field and new_field
    assert "base_field" in log["entries"]
    assert "new_field" in log["entries"]
    assert log["entries"]["new_field"] is None

    # computed_field should NOT be in entries (it's in derived_entries)
    assert "computed_field" not in log["entries"]

    # But it should be in derived_entries
    assert "computed_field" in log["derived_entries"]
    assert log["derived_entries"]["computed_field"] == 20  # 10 * 2


@pytest.mark.anyio
async def test_unique_field_constraint(client: AsyncClient):
    """Test that the unique constraint on fields is enforced."""
    project_name = "test-unique-field"
    await _create_project(client, project_name)

    # Create a field with a unique constraint via the /fields endpoint
    fields_data = {
        "project_name": project_name,
        "fields": {"email": {"type": "str", "unique": True}},
    }
    response = await client.post(
        "/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create a log, which should succeed
    response1 = await _create_log(
        client,
        project_name,
        entries={"email": "test@example.com"},
    )
    assert response1.status_code == 200, response1.json()

    # Try to create another log with the same email, which should fail
    response2 = await _create_log(
        client,
        project_name,
        entries={"email": "test@example.com"},
    )
    assert response2.status_code == 400, response2.json()
    assert "Duplicate entry for unique field" in response2.json()["detail"]

    # Try to create a log with a different email, which should succeed
    response3 = await _create_log(
        client,
        project_name,
        entries={"email": "another@example.com"},
    )
    assert response3.status_code == 200, response3.json()


@pytest.mark.anyio
async def test_unique_field_constraint_on_log_creation(
    client: AsyncClient,
):
    """Test creating a unique field during log creation."""
    project_name = "test-unique-on-creation"
    await _create_project(client, project_name)

    # Create a log with a unique field defined in explicit_types
    entries1 = {
        "user_id": "user-123",
        "explicit_types": {"user_id": {"type": "str", "unique": True}},
    }
    response1 = await _create_log(client, project_name, entries=entries1)
    assert response1.status_code == 200, response1.json()

    # Try to create another log with the same user_id, which should fail
    entries2 = {"user_id": "user-123"}
    response2 = await _create_log(client, project_name, entries=entries2)
    assert response2.status_code == 400, response2.json()
    assert "Duplicate entry for unique field" in response2.json()["detail"]


@pytest.mark.anyio
async def test_unique_field_constraint_on_update(client: AsyncClient):
    """Test that the unique constraint on fields is enforced during update."""
    project_name = "test-unique-on-update"
    await _create_project(client, project_name)

    # Create a log with a unique field and another log to be updated
    await _create_log(
        client,
        project_name,
        entries={
            "email": "unique@example.com",
            "explicit_types": {
                "email": {"type": "str", "unique": True, "mutable": True},
            },
        },
    )

    response = await _create_log(
        client,
        project_name,
        entries={"email": "tobeupdated@example.com"},
    )
    assert response.status_code == 200, response.json()
    log_id_to_update = response.json()["log_event_ids"][0]

    # Try to update the second log to have the same email as the first, should fail
    update_response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id_to_update],
            "project_name": project_name,
            "entries": {"email": "unique@example.com"},
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert update_response.status_code == 400, update_response.json()
    assert "Duplicate entry for unique field" in update_response.json()["detail"]

    # Try to update to a new unique email, should succeed
    update_response_success = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id_to_update],
            "project_name": project_name,
            "entries": {"email": "newunique@example.com"},
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert update_response_success.status_code == 200, update_response_success.json()


@pytest.mark.anyio
async def test_field_description_crud(client: AsyncClient):
    """Test creating fields with descriptions and verifying CRUD operations."""
    project_name = "test-field-description"
    await _create_project(client, project_name)

    # Create fields with and without descriptions via POST /logs/fields
    fields_data = {
        "project_name": project_name,
        "fields": {
            "field_with_description": {
                "type": "str",
                "description": "This field has a description",
            },
            "field_without_description": {
                "type": "int",
                # No description provided
            },
        },
    }

    response = await client.post(
        "/v0/logs/fields",
        json=fields_data,
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert "Fields created successfully" in response.json()["info"]

    # Fetch fields via GET /logs/fields and verify descriptions
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200, fields_response.json()
    fields = fields_response.json()

    # Verify field with description
    assert "field_with_description" in fields
    assert fields["field_with_description"]["data_type"] == "str"
    assert (
        fields["field_with_description"]["description"]
        == "This field has a description"
    )

    # Verify field without description returns null/None
    assert "field_without_description" in fields
    assert fields["field_without_description"]["data_type"] == "int"
    assert fields["field_without_description"]["description"] is None


@pytest.mark.anyio
async def test_update_field_endpoint_description_only(client: AsyncClient):
    """Test updating and clearing field descriptions via update_field."""
    project_name = "test-update-field-description"
    await _create_project(client, project_name)

    create_response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "score": {
                    "type": "float",
                    "mutable": True,
                    "description": "Original description",
                },
            },
        },
        headers=HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()

    update_response = await client.patch(
        "/v0/logs/update_field",
        json={
            "project_name": project_name,
            "field_name": "score",
            "description": "Updated description",
        },
        headers=HEADERS,
    )
    assert update_response.status_code == 200, update_response.json()

    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200, fields_response.json()
    fields = fields_response.json()

    assert fields["score"]["description"] == "Updated description"
    assert fields["score"]["data_type"] == "float"
    assert fields["score"]["mutable"] is True

    clear_response = await client.patch(
        "/v0/logs/update_field",
        json={
            "project_name": project_name,
            "field_name": "score",
            "description": None,
        },
        headers=HEADERS,
    )
    assert clear_response.status_code == 200, clear_response.json()

    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200, fields_response.json()
    assert fields_response.json()["score"]["description"] is None


@pytest.mark.anyio
async def test_update_field_missing_field(client: AsyncClient):
    """Test updating a missing field returns 404."""
    project_name = "test-update-missing-field-description"
    await _create_project(client, project_name)

    response = await client.patch(
        "/v0/logs/update_field",
        json={
            "project_name": project_name,
            "field_name": "missing_field",
            "description": "Does not exist",
        },
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert "does not exist" in response.json()["detail"]


@pytest.mark.anyio
async def test_explicit_nested_type_in_get_fields(client: AsyncClient):
    """Test that get_fields returns explicit nested types correctly."""
    project_name = "test-get-fields-nested"
    await _create_project(client, project_name)

    # Create a log with nested explicit type
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "values": [1, 2, 3],
                "metrics": {"acc": 0.9, "loss": 0.1},
                "explicit_types": {
                    "values": {"type": "List[int]", "mutable": True},
                    "metrics": {"type": "Dict[str, float]", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Get fields and verify they return the explicit types
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()

    # Should return the explicit nested types, not the base types
    assert fields["values"]["data_type"] == "List[int]"
    assert fields["metrics"]["data_type"] == "Dict[str, float]"


@pytest.mark.anyio
async def test_explicit_type_overrides_in_fields(client: AsyncClient):
    """Test that explicit types override inferred types in get_fields."""
    project_name = "test-explicit-override-fields"
    await _create_project(client, project_name)

    # Create a log with explicit type overriding inference
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "recording_url": "",
                "explicit_types": {
                    "recording_url": {"type": "str", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Get fields and verify explicit type is returned
    fields_response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert fields_response.status_code == 200
    fields = fields_response.json()

    # Should return "str", not "audio" or "Any"
    assert fields["recording_url"]["data_type"] == "str"


@pytest.mark.anyio
async def test_get_fields_all_contexts_with_wildcard(
    client: AsyncClient,
):
    """Test that using context='*' returns fields from all contexts in the project."""
    project_name = "test-get-fields-all-contexts"
    context_name_1 = "ctx1"
    context_name_2 = "ctx2"

    # Create project and two contexts
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name_1, "description": "First context"},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name_2, "description": "Second context"},
        headers=HEADERS,
    )

    # Create multiple fields in each context via POST /logs/fields
    resp_ctx1 = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "context": context_name_1,
            "fields": {
                "accuracy_ctx1": "float",
                "loss_ctx1": "float",
            },
        },
        headers=HEADERS,
    )
    assert resp_ctx1.status_code == 200, resp_ctx1.json()

    resp_ctx2 = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "context": context_name_2,
            "fields": {
                "accuracy_ctx2": "float",
                "loss_ctx2": "float",
            },
        },
        headers=HEADERS,
    )
    assert resp_ctx2.status_code == 200, resp_ctx2.json()

    # Fetch fields across all contexts using the wildcard
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}&context=*",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields_by_context = response.json()

    # Both contexts should be present as top-level keys
    assert context_name_1 in fields_by_context
    assert context_name_2 in fields_by_context

    # Each context should contain its respective fields
    assert "accuracy_ctx1" in fields_by_context[context_name_1]
    assert "loss_ctx1" in fields_by_context[context_name_1]
    assert "accuracy_ctx2" in fields_by_context[context_name_2]
    assert "loss_ctx2" in fields_by_context[context_name_2]
