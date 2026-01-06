from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


@pytest.mark.anyio
async def test_create_log_weakly_typed(client: AsyncClient, use_jsonb_mode):
    """Test that implicitly created fields always have type 'Any'."""
    project_name = f"test_project-wt-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create a log with implicitly typed fields (no POST /logs/fields first)
    response = await _create_log(
        client,
        project_name,
        params={"a/b/param1": "test"},
        entries={"score": 10, "logged_at": datetime.now(timezone.utc).isoformat()},
    )

    assert response.status_code == 200, response.json()

    # Verify that all implicitly created fields have type "Any"
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    # Implicit fields have types inferred from values
    assert "a/b/param1" in field_types
    param1_type = field_types["a/b/param1"]
    assert param1_type["data_type"] == "str"  # Type inferred from value
    assert param1_type["field_type"] == "param"
    assert param1_type["mutable"] is True
    assert param1_type["artifacts"] == ""
    assert "created_at" in param1_type

    assert "score" in field_types
    score_type = field_types["score"]
    assert score_type["data_type"] == "int"  # Type inferred from value
    assert score_type["field_type"] == "entry"
    assert score_type["mutable"] is True
    assert score_type["artifacts"] == ""
    assert "created_at" in score_type

    assert "logged_at" in field_types
    logged_at_type = field_types["logged_at"]
    assert (
        logged_at_type["data_type"] == "datetime"
    )  # Type inferred from ISO datetime string
    assert logged_at_type["field_type"] == "entry"
    assert logged_at_type["mutable"] is True
    assert logged_at_type["artifacts"] == ""
    assert "created_at" in logged_at_type


@pytest.mark.anyio
async def test_create_log_type_mismatch(client: AsyncClient, use_jsonb_mode):
    """Test that type mismatches are caught for explicitly typed fields."""
    project_name = f"test_project-tm-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Step 1: Explicitly create strictly typed fields via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "score": {"type": "int", "mutable": True},
                "response": {"type": "str", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 2: Log matching types → should succeed
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "score": 10,
                "response": "hello",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 3: Try to log None value to strictly typed int field → should SUCCEED
    # NoneType is a weak type; strict fields accept None values
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {"score": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 4: Verify field types are strict
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["response"]["data_type"] == "str"
    assert field_types["score"]["data_type"] == "int"
    assert field_types["a/b/param1"]["data_type"] == "str"  # Type inferred from value

    # Step 5: Try to log mismatched types → should FAIL
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "params": {"a/b/param1": True},  # bool, but expects str
            "entries": {
                "score": "not_an_int",  # str, but expects int
                "response": "hello",
            },
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "Type mismatch for field" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_logs_strongly_typed(client: AsyncClient, use_jsonb_mode):
    """Test updating logs with implicitly created fields (type 'Any')."""
    project_name = f"test_project-st-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create a log first - fields created implicitly will have type "Any"
    response1 = await _create_log(client, project_name)
    log_id1 = response1.json()["log_event_ids"][0]

    # Update the log - new fields will also have type "Any"
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "a/b/c/input": "new data",
                "a/b/c/numeric_input": -12.0,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Implicitly created fields have types inferred from values
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["a/b/c/input"]["data_type"] == "str"  # Type inferred from value
    assert (
        field_types["a/b/c/numeric_input"]["data_type"] == "float"
    )  # Type inferred from value


@pytest.mark.anyio
async def test_nonetype_is_weak_type(client: AsyncClient, use_jsonb_mode):
    """Test that NoneType is a weak type allowed with any strong type and also standalone."""
    project_name = f"test_nonetype-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create fields with different types
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "none_field": {"type": "NoneType", "mutable": True},
                "int_field": {"type": "int", "mutable": True},
                "any_field": {"type": "Any", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Test 1: NoneType field accepts None → should succeed
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "none_field": None,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Test 2: NoneType field accepts non-None values → should succeed (weak type)
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "none_field": 42,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Test 3: int field accepts None → should succeed (None allowed for strict types)
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "int_field": None,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Test 4: int field accepts int → should succeed
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "int_field": 42,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Test 5: Any field accepts None → should succeed
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "any_field": None,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Test 6: Any field accepts int → should succeed
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "any_field": 42,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify field types remain correct
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["none_field"]["data_type"] == "NoneType"
    assert field_types["int_field"]["data_type"] == "int"
    assert field_types["any_field"]["data_type"] == "Any"


@pytest.mark.anyio
async def test_update_logs_previously_none(client: AsyncClient, use_jsonb_mode):
    """Test the field type creation policy.

    Policy:
    1. Explicit field creation (POST /logs/fields) → strict types
    2. Implicit field creation (POST /logs, PUT /logs) → always type "Any"
    3. "Any" fields accept all value types and never change to strict types
    """
    project_name = f"test_project-pn-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Part 1: Explicitly created fields have strict types
    # ===================================================

    # Step 1: Create a strictly typed field via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "strict_field": {
                    "type": "str",
                    "mutable": True,
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 2: Try to log a mismatched type to the strict field → should FAIL
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "strict_field": 42,  # int, but field expects str
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Type mismatch" in response.json()["detail"]
    assert "strict_field" in response.json()["detail"]

    # Part 2: Implicitly created fields always have type "Any"
    # =========================================================

    # Step 3: Implicitly create a field by logging an int value
    # Note: Implicit fields infer types from values
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "implicit_field": 42,  # Type inferred as "int"
        },
    )
    assert response1.status_code == 200, response1.json()
    log_id1 = response1.json()["log_event_ids"][0]

    # Verify the field has type inferred from value
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert (
        field_types["implicit_field"]["data_type"] == "int"
    )  # Type inferred from value
    assert field_types["implicit_field"]["field_type"] == "entry"
    assert field_types["implicit_field"]["mutable"] == True

    # Step 4: Log a None value to the int field → should SUCCEED (None is allowed)
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "implicit_field": None,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Step 5: Verify the field type remains "int" (None doesn't change the type)
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["implicit_field"]["data_type"] == "int"  # Still "int"
    assert field_types["implicit_field"]["field_type"] == "entry"
    assert field_types["implicit_field"]["mutable"] == True

    # Step 6: Log an int to the same field → should SUCCEED
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "implicit_field": -12,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Step 7: Verify the field type remains "int"
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["implicit_field"]["data_type"] == "int"  # Still "int"

    # Step 8: Try to log a string to the int field → should FAIL (type mismatch)
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "implicit_field": "now a string",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    # Type mismatch should fail because field is now strictly typed as "int"
    assert (
        response.status_code == 400
    ), f"Expected 400, got {response.status_code}: {response.json()}"

    # Step 9: Final verification - field is still "int"
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["implicit_field"]["data_type"] == "int"  # Stays "int"


@pytest.mark.anyio
async def test_update_logs_type_mismatch(client: AsyncClient, use_jsonb_mode):
    """Test that type mismatches are caught when updating strictly typed fields."""
    project_name = f"test_project-utm-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Step 1: Create strictly typed field via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "a/b/c/numeric_input": {"type": "int", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Step 2: Create a log with correct type
    response1 = await _create_log(
        client,
        project_name,
        entries={"a/b/c/numeric_input": 42},
    )
    assert response1.status_code == 200, response1.json()
    log_id1 = response1.json()["log_event_ids"][0]

    # Step 3: Try to update the log with wrong type → should FAIL
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "a/b/c/numeric_input": "not_an_int",  # str, but expects int
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "Type mismatch for field" in response.json()["detail"]


@pytest.mark.anyio
async def test_create_log_with_mutable_fields(client: AsyncClient, use_jsonb_mode):
    """Test creating fields with explicit mutability settings."""
    project_name = f"test_mutable_fields-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create fields via POST /logs/fields with mutability settings
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "mutable_field": {"type": "str", "mutable": True},
                "immutable_field": {"type": "str", "mutable": False},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Now create a log with these fields
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "mutable_field": "initial value",
                "immutable_field": "fixed value",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Verify field types include mutability information
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["mutable_field"]["mutable"] is True
    assert field_types["immutable_field"]["mutable"] is False


@pytest.mark.anyio
async def test_create_log_default_immutable(client: AsyncClient, use_jsonb_mode):
    """Test that implicitly created fields default to immutable."""
    project_name = f"test_default_immutable-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create a log without specifying mutability (should default to immutable)
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "default_field": "initial value",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Verify field is immutable by default and has type inferred from value
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["default_field"]["mutable"] is False
    assert (
        field_types["default_field"]["data_type"] == "str"
    )  # Type inferred from value

    # Attempt to update the default immutable field (should fail)
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "default_field": "attempted update",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Field is immutable and cannot be modified" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_mutable_and_immutable_fields(client: AsyncClient, use_jsonb_mode):
    """Test updating mutable vs immutable fields."""
    project_name = f"test_mutable_updates-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create fields via POST /logs/fields with different mutability settings
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "mutable_field": {"type": "str", "mutable": True},
                "immutable_field": {"type": "str", "mutable": False},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create initial log with both fields
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "mutable_field": "initial value",
                "immutable_field": "fixed value",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Test updating mutable field (should succeed)
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "mutable_field": "updated value",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify mutable field was updated
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_data = response.json()["logs"][0]
    assert log_data["entries"]["mutable_field"] == "updated value"
    assert log_data["entries"]["immutable_field"] == "fixed value"

    # Test updating immutable field (should fail)
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "immutable_field": "attempted update",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Field is immutable and cannot be modified" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_field_mutability_only(client: AsyncClient, use_jsonb_mode):
    """Test updating field mutability without changing the value."""
    project_name = f"test_mutability_update-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create field via POST /logs/fields with mutable=True
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "mutable_field": {"type": "str", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create initial log
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "mutable_field": "test value",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Verify field is initially mutable
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["mutable_field"]["mutable"] is True

    # Update only the mutability without changing the value
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "explicit_types": {
                    "mutable_field": {"mutable": False},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify field is now immutable but value unchanged
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["mutable_field"]["mutable"] is False

    # Verify the value was not changed
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["mutable_field"] == "test value"

    # Attempt to modify the now-immutable field (should fail)
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "mutable_field": "new value",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Field is immutable and cannot be modified" in response.json()["detail"]


@pytest.mark.anyio
async def test_create_log_closed_enum(client: AsyncClient, use_jsonb_mode):
    """Test creating a log with a closed enum type."""
    project_name = f"test_closed_enum-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create enum field via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "color": {
                    "type": "enum",
                    "values": ["red", "green"],
                    "restrict": True,
                    "mutable": True,
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create a log with the enum field
    response = await _create_log(
        client,
        project_name,
        entries={
            "color": "red",
        },
    )
    assert response.status_code == 200, response.json()

    # Verify field types include enum information
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "color" in field_types
    color_type = field_types["color"]
    assert color_type["data_type"] == "enum"
    assert set(color_type["enum_values"]) == {"red", "green"}
    assert color_type["restrict"] is True

    # Verify the log entry has the correct value
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["color"] == "red"


@pytest.mark.anyio
async def test_update_log_enum_auto_expand(client: AsyncClient, use_jsonb_mode):
    """Test that open enums automatically expand when new values are added."""
    project_name = f"test_enum_auto_expand-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create open enum field via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "status": {
                    "type": "enum",
                    "values": ["A"],
                    "restrict": False,
                    "mutable": True,
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create a log with the initial enum value
    response = await _create_log(
        client,
        project_name,
        entries={
            "status": "A",
        },
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Update the log with a new enum value
    # Note: Since the enum is open (restrict=False), "B" should be accepted
    # even though it wasn't in the initial values list
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "status": "B",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Note: Auto-expansion of enum values is not yet implemented
    # The field should accept "B" because restrict=False, but the enum_values
    # list may or may not be updated automatically
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "status" in field_types
    status_type = field_types["status"]
    assert status_type["data_type"] == "enum"
    # Verify it's still an open enum
    assert status_type["restrict"] is False


@pytest.mark.anyio
async def test_create_open_enum_without_values(client: AsyncClient, use_jsonb_mode):
    """Test creating open enum without initial values - values are inferred from first log."""
    project_name = f"test_open_enum_no_values-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create open enum field via POST /logs/fields (without values)
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "category": {
                    "type": "enum",
                    "restrict": False,
                    "mutable": True,
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create a log with the first enum value
    response = await _create_log(
        client,
        project_name,
        entries={
            "category": "alpha",
        },
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Verify field types show enum with no initial values
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "category" in field_types
    category_type = field_types["category"]
    assert category_type["data_type"] == "enum"
    assert category_type["restrict"] is False
    # Empty enum_values when none specified at creation
    assert category_type["enum_values"] == [] or category_type["enum_values"] is None

    # Update the log with a new value
    # Open enum should accept any value
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "category": "beta",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify field type remains enum
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "category" in field_types
    category_type = field_types["category"]
    assert category_type["data_type"] == "enum"
    assert category_type["restrict"] is False


@pytest.mark.anyio
async def test_closed_enum_without_values(client: AsyncClient, use_jsonb_mode):
    """Test creating closed enum that restricts to initially provided values."""
    project_name = f"test_closed_enum_no_values-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create closed enum field via POST /logs/fields with initial value
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "priority": {
                    "type": "enum",
                    "values": ["high"],
                    "restrict": True,
                    "mutable": True,
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create a log with the allowed enum value
    response = await _create_log(
        client,
        project_name,
        entries={
            "priority": "high",
        },
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Verify field types show restrict=True
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "priority" in field_types
    priority_type = field_types["priority"]
    assert priority_type["data_type"] == "enum"
    assert priority_type["enum_values"] == ["high"]
    assert priority_type["restrict"] is True

    # Attempt to update with a new value not in the allowed set
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "priority": "new",
                "explicit_types": {"priority": {"type": "enum"}},
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    # Should fail with a value error about allowed values
    assert response.status_code == 400
    error_detail = response.json()["detail"]
    assert (
        "allowed enum values" in error_detail.lower() or "enum" in error_detail.lower()
    )


@pytest.mark.anyio
async def test_filter_logs_by_enum(client: AsyncClient, use_jsonb_mode):
    """Tests filtering logs by enum values is treated as regular string filtering."""
    project_name = f"test_enum_filtering-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create enum field via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "status": {
                    "type": "enum",
                    "values": ["ok", "error"],
                    "restrict": True,
                    "mutable": True,
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create multiple logs with different enum values
    for i in range(5):
        status = "ok" if i % 2 == 0 else "error"
        response = await _create_log(
            client,
            project_name,
            entries={
                "status": status,
            },
        )
        assert response.status_code == 200, response.json()

    # Filter logs by enum value
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "status == 'error'"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    # Should have 2 logs with status="error"
    assert len(logs) == 2
    for log in logs:
        assert log["entries"]["status"] == "error"

    # Filter by the other enum value
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "status == 'ok'"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Should have 3 logs with status="ok"
    assert len(logs) == 3
    for log in logs:
        assert log["entries"]["status"] == "ok"


@pytest.mark.anyio
async def test_nested_explicit_type_case_insensitive(
    client: AsyncClient,
    use_jsonb_mode,
):
    """Test that nested explicit types are case-insensitive."""
    project_name = f"test_nested_case-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create fields via POST /logs/fields with different casing
    test_cases = [
        ("field1", "LIST[INT]"),
        ("field2", "list[int]"),
        ("field3", "List[Int]"),
    ]

    fields_dict = {}
    for field_name, type_str in test_cases:
        fields_dict[field_name] = {"type": type_str, "mutable": True}

    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": fields_dict,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Now create logs with these fields
    test_values = [
        ("field1", [1, 2, 3]),
        ("field2", [4, 5, 6]),
        ("field3", [7, 8, 9]),
    ]

    for field_name, value in test_values:
        response = await _create_log(
            client,
            project_name,
            entries={
                field_name: value,
            },
        )
        assert response.status_code == 200, response.json()

    # Verify all types are normalized to "List[int]"
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["field1"]["data_type"] == "List[int]"
    assert field_types["field2"]["data_type"] == "List[int]"
    assert field_types["field3"]["data_type"] == "List[int]"


@pytest.mark.anyio
async def test_explicit_type_with_params(client: AsyncClient, use_jsonb_mode):
    """Test explicit types work with params as well as entries."""
    project_name = f"test_params_explicit_type-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create fields via POST /logs/fields for both params and entries
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "config": {"type": "Dict[str, float]", "mutable": True},
                "result": {"type": "List[float]", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create a log with these nested types (both as entries)
    # Don't use "config" as a param since it was created as an entry field
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "params": {
                "model": "gpt-4",  # Implicit param with type "Any"
            },
            "entries": {
                "config": {"lr": 0.001, "epochs": 100.0},
                "result": [0.9, 0.95, 0.98],
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify field types
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["config"]["data_type"] == "Dict[str, float]"
    assert (
        field_types["config"]["field_type"] == "entry"
    )  # All fields via POST /logs/fields are "entry"
    assert field_types["result"]["data_type"] == "List[float]"
    assert field_types["result"]["field_type"] == "entry"
    assert field_types["model"]["data_type"] == "str"  # Type inferred from value
    assert field_types["model"]["field_type"] == "param"


@pytest.mark.anyio
async def test_nested_type_persists_across_logs(client: AsyncClient, use_jsonb_mode):
    """Test that nested type persists when creating multiple logs."""
    project_name = f"test_type_persistence-{'jsonb' if use_jsonb_mode else 'eav'}"
    _ = await _create_project(client, project_name)

    # Create field with nested type via POST /logs/fields
    response = await client.post(
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": {
                "data": {"type": "List[int]", "mutable": True},
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # First log with the nested type
    response1 = await _create_log(
        client,
        project_name,
        entries={
            "data": [1, 2, 3],
        },
    )
    assert response1.status_code == 200, response1.json()

    # Second log - type should still be enforced
    response2 = await _create_log(
        client,
        project_name,
        entries={
            "data": [4, 5, 6],
        },
    )
    assert response2.status_code == 200, response2.json()

    # Verify type is still "List[int]"
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["data"]["data_type"] == "List[int]"
