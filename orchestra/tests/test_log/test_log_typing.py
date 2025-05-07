from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from . import HEADERS, _create_log, _create_project


@pytest.mark.anyio
async def test_create_log_strongly_typed(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log with strongly typed fields
    response = await _create_log(
        client,
        project_name,
        params={"a/b/param1": "test"},
        entries={"score": 10, "logged_at": datetime.now(timezone.utc).isoformat()},
    )

    assert response.status_code == 200, response.json()

    # Verify that field types are set correctly
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    # Verify field types and their properties
    assert "a/b/param1" in field_types
    param1_type = field_types["a/b/param1"]
    assert param1_type["data_type"] == "str"
    assert param1_type["field_type"] == "param"
    assert param1_type["mutable"] is True
    assert param1_type["artifacts"] == ""
    assert "created_at" in param1_type

    assert "score" in field_types
    score_type = field_types["score"]
    assert score_type["data_type"] == "int"
    assert score_type["field_type"] == "entry"
    assert score_type["mutable"] is True
    assert score_type["artifacts"] == ""
    assert "created_at" in score_type

    assert "logged_at" in field_types
    logged_at_type = field_types["logged_at"]
    assert logged_at_type["data_type"] == "timestamp"
    assert logged_at_type["field_type"] == "entry"
    assert logged_at_type["mutable"] is True
    assert logged_at_type["artifacts"] == ""
    assert "created_at" in logged_at_type


@pytest.mark.anyio
async def test_create_log_type_mismatch(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "score": 10,
                "response": "hello",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    # Create a log with a None value (should work)
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {"score": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    # get field types
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["response"]["data_type"] == "str"
    assert field_types["score"]["data_type"] == "int"
    assert field_types["response"]["data_type"] == "str"
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": True},  # This should cause a type mismatch
            "entries": {
                "score": "not_an_int",
                "response": "hello",
            },
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "Type mismatch for field" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_logs_strongly_typed(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log first
    response1 = await _create_log(client, project_name)
    log_id1 = response1.json()["log_event_ids"][0]

    # Update the log with strongly typed fields
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


@pytest.mark.anyio
async def test_update_logs_previously_none(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log first
    response1 = await _create_log(
        client,
        project_name,
        params={"a/b/param1": "test"},
        entries={
            "a/b/c/input": "Some input data",
            "a/b/c/boolean_input": True,
            "a/b/c/numeric_input": None,
        },
    )
    log_id1 = response1.json()["log_event_ids"][0]

    # Verify numeric is NoneType
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json()["a/b/c/numeric_input"]["data_type"] == "NoneType"
    assert field_types_response.json()["a/b/c/numeric_input"]["field_type"] == "entry"
    assert field_types_response.json()["a/b/c/numeric_input"]["mutable"] == True
    assert field_types_response.json()["a/b/c/numeric_input"]["artifacts"] == ""
    assert field_types_response.json()["a/b/c/numeric_input"]["created_at"] is not None

    # Update the log with strongly typed fields, but previously None
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "a/b/c/numeric_input": -12.0,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify numeric is now float
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json()["a/b/c/numeric_input"]["data_type"] == "float"
    assert field_types_response.json()["a/b/c/numeric_input"]["field_type"] == "entry"
    assert field_types_response.json()["a/b/c/numeric_input"]["mutable"] == True
    assert field_types_response.json()["a/b/c/numeric_input"]["artifacts"] == ""
    assert field_types_response.json()["a/b/c/numeric_input"]["created_at"] is not None

    # Now update the field back to None and verify it works
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "a/b/c/numeric_input": None,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify numeric is still float type (since type was established)
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json()["a/b/c/numeric_input"]["data_type"] == "float"
    assert field_types_response.json()["a/b/c/numeric_input"]["field_type"] == "entry"
    assert field_types_response.json()["a/b/c/numeric_input"]["mutable"] == True
    assert field_types_response.json()["a/b/c/numeric_input"]["artifacts"] == ""
    assert field_types_response.json()["a/b/c/numeric_input"]["created_at"] is not None


@pytest.mark.anyio
async def test_update_logs_type_mismatch(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log first
    response1 = await _create_log(client, project_name)
    log_id1 = response1.json()["log_event_ids"][0]

    # Update the log with a type mismatch
    response = await client.put(
        f"/v0/logs",
        json={
            "logs": [log_id1],
            "entries": {
                "a/b/c/numeric_input": "not_an_int",  # This should cause a type mismatch
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "Type mismatch for field" in response.json()["detail"]


@pytest.mark.anyio
async def test_create_log_with_mutable_fields(client: AsyncClient):
    project_name = "test_mutable_fields"
    _ = await _create_project(client, project_name)

    # Create a log with both mutable and immutable fields using explicit_types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "mutable_field": "initial value",
                "immutable_field": "fixed value",
                "explicit_types": {
                    "mutable_field": {"type": "str", "mutable": True},
                    "immutable_field": {"type": "str", "mutable": False},
                },
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
async def test_create_log_default_immutable(client: AsyncClient):
    project_name = "test_default_immutable"
    _ = await _create_project(client, project_name)

    # Create a log without specifying mutability (should default to immutable)
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "default_field": "initial value",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Verify field is immutable by default
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["default_field"]["mutable"] is False

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
async def test_update_mutable_and_immutable_fields(client: AsyncClient):
    project_name = "test_mutable_updates"
    _ = await _create_project(client, project_name)

    # Create initial log with mutable and immutable fields using explicit_types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "mutable_field": "initial value",
                "immutable_field": "fixed value",
                "explicit_types": {
                    "mutable_field": {"type": "str", "mutable": True},
                    "immutable_field": {"type": "str", "mutable": False},
                },
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
async def test_update_field_mutability_only(client: AsyncClient):
    """Test updating field mutability without changing the value."""
    project_name = "test_mutability_update"
    _ = await _create_project(client, project_name)

    # Create initial log with a mutable field
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "mutable_field": "test value",
                "explicit_types": {
                    "mutable_field": {"type": "str", "mutable": True},
                },
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
async def test_create_log_closed_enum(client: AsyncClient):
    """Test creating a log with a closed enum type."""
    project_name = "test_closed_enum"
    _ = await _create_project(client, project_name)

    # Create a log with a closed enum field
    response = await _create_log(
        client,
        project_name,
        entries={
            "color": "red",
            "explicit_types": {
                "color": {"type": "enum", "values": ["red", "green"], "restrict": True},
            },
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
    assert color_type["data_type"] == "str"
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
async def test_update_log_enum_auto_expand(client: AsyncClient):
    """Test that open enums automatically expand when new values are added."""
    project_name = "test_enum_auto_expand"
    _ = await _create_project(client, project_name)

    # Create a log with an open enum field
    response = await _create_log(
        client,
        project_name,
        entries={
            "status": "A",
            "explicit_types": {
                "status": {
                    "type": "enum",
                    "values": ["A"],
                    "restrict": False,
                    "mutable": True,
                },
            },
        },
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Update the log with a new enum value
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "status": "B",
                "explicit_types": {"status": {"type": "enum", "restrict": False}},
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify field types now include both enum values
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "status" in field_types
    status_type = field_types["status"]
    assert status_type["data_type"] == "str"
    assert set(status_type["enum_values"]) == {"A", "B"}
    assert status_type["restrict"] is False


@pytest.mark.anyio
async def test_create_open_enum_without_values(client: AsyncClient):
    """Test creating a log with an open enum type that omits the values key entirely."""
    project_name = "test_open_enum_no_values"
    _ = await _create_project(client, project_name)

    # Create a log with an open enum field that omits the values key
    response = await _create_log(
        client,
        project_name,
        entries={
            "category": "alpha",
            "explicit_types": {
                "category": {"type": "enum", "restrict": False, "mutable": True},
            },
        },
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Verify field types show "alpha" in enum_values and restrict=False
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "category" in field_types
    category_type = field_types["category"]
    assert category_type["data_type"] == "str"
    assert category_type["enum_values"] == ["alpha"]
    assert category_type["restrict"] is False

    # Update the log with a new value without specifying values key
    response = await client.put(
        "/v0/logs",
        json={
            "logs": [log_id],
            "entries": {
                "category": "beta",
                "explicit_types": {"category": {"type": "enum", "restrict": False}},
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify field types now include both values
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "category" in field_types
    category_type = field_types["category"]
    assert category_type["data_type"] == "str"
    assert category_type["enum_values"] == ["alpha", "beta"]
    assert category_type["restrict"] is False


@pytest.mark.anyio
async def test_closed_enum_without_values(client: AsyncClient):
    """Test creating a log with a closed enum type that omits the values key."""
    project_name = "test_closed_enum_no_values"
    _ = await _create_project(client, project_name)

    # Create a log with a closed enum field that omits the values key
    # First write should seed the enum with the initial value
    response = await _create_log(
        client,
        project_name,
        entries={
            "priority": "high",
            "explicit_types": {
                "priority": {"type": "enum", "restrict": True, "mutable": True},
            },
        },
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    # Verify field types show the seeded value and restrict=True
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert "priority" in field_types
    priority_type = field_types["priority"]
    assert priority_type["data_type"] == "str"
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
    assert "allowed enum values" in error_detail.lower()


@pytest.mark.anyio
async def test_filter_logs_by_enum(client: AsyncClient):
    """Tests filtering logs by enum values is treated as regular string filtering."""
    project_name = "test_enum_filtering"
    _ = await _create_project(client, project_name)

    # Create multiple logs with different enum values
    for i in range(5):
        status = "ok" if i % 2 == 0 else "error"
        response = await _create_log(
            client,
            project_name,
            entries={
                "status": status,
                "explicit_types": {
                    "status": {
                        "type": "enum",
                        "values": ["ok", "error"],
                        "restrict": True,
                    },
                },
            },
        )
        assert response.status_code == 200, response.json()

    # Filter logs by enum value
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "status == 'error'"},
        headers=HEADERS,
    )
    assert response.status_code == 200
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
