"""Tests for CASCADE, SET NULL, and SET DEFAULT foreign key actions."""

import pytest
from httpx import AsyncClient

from .test_log import HEADERS, _create_project


async def _get_logs(client: AsyncClient, project_name: str, context: str = None):
    """Helper to get logs from a project/context."""
    params = {"project": project_name}
    if context:
        params["context"] = context
    return await client.get("/v0/logs", params=params, headers=HEADERS)


@pytest.mark.anyio
async def test_cascade_delete_removes_referencing_rows(client: AsyncClient):
    """Test that CASCADE DELETE automatically deletes referencing rows."""
    project_name = "cascade-delete-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "is_versioned": True,
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "entries": {"name": "Engineering"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_id = response.json()["row_ids"]["ids"][0][0]
    dept_log_id = response.json()["log_event_ids"][0]

    # Create Employees context with CASCADE FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create employees referencing the department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": [
                {"name": "Alice", "department_id": dept_id},
                {"name": "Bob", "department_id": dept_id},
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify employees were created
    response = await _get_logs(client, project_name, context="Employees")
    assert response.status_code == 200
    assert response.json()["count"] == 2

    # Delete the department - should CASCADE delete employees
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify department is deleted
    response = await _get_logs(client, project_name, context="Departments")
    assert response.status_code == 200
    assert response.json()["count"] == 0

    # Verify employees were CASCADE deleted
    response = await _get_logs(client, project_name, context="Employees")
    assert response.status_code == 200
    assert response.json()["count"] == 0


@pytest.mark.anyio
async def test_cascade_update_propagates_changes(client: AsyncClient):
    """Test that CASCADE UPDATE propagates changes to referencing rows."""
    project_name = "cascade-update-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "is_versioned": True,
            "mutable_keys": ["id"],  # Allow id to be updated
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a department with explicit id
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "entries": {"id": 1, "name": "Engineering"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_log_id = response.json()["log_event_ids"][0]

    # Create Employees context with CASCADE FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "NO ACTION",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create employees referencing the department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": [
                {"name": "Alice", "department_id": 1},
                {"name": "Bob", "department_id": 1},
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Update the department id - should CASCADE update employees
    response = await client.put(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "logs": [dept_log_id],
            "entries": {"id": 100},  # Change id from 1 to 100
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify employees were CASCADE updated
    response = await _get_logs(client, project_name, context="Employees")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 2
    for log in logs:
        assert log["entries"]["department_id"] == 100


@pytest.mark.anyio
async def test_set_null_on_delete(client: AsyncClient):
    """Test that SET NULL sets FK to NULL when referenced row is deleted."""
    project_name = "set-null-delete-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "is_versioned": True,
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "entries": {"name": "Engineering"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_id = response.json()["row_ids"]["ids"][0][0]
    dept_log_id = response.json()["log_event_ids"][0]

    # Create Employees context with SET NULL FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "SET NULL",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create employees referencing the department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": [
                {"name": "Alice", "department_id": dept_id},
                {"name": "Bob", "department_id": dept_id},
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Delete the department - should SET NULL on employees
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify employees still exist but department_id is NULL (not present)
    response = await _get_logs(client, project_name, context="Employees")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 2
    for log in logs:
        assert "department_id" not in log["entries"]  # NULL = not present
        assert "name" in log["entries"]  # But name still exists


@pytest.mark.anyio
async def test_cascade_delete_multi_level(client: AsyncClient):
    """Test CASCADE DELETE with chained foreign keys (A→B→C)."""
    project_name = "cascade-multi-level-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "is_versioned": True,
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "entries": {"name": "Engineering"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_id = response.json()["row_ids"]["ids"][0][0]
    dept_log_id = response.json()["log_event_ids"][0]

    # Create Employees context with CASCADE FK to Departments
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create an employee
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": {"name": "Alice", "department_id": dept_id},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    emp_id = response.json()["row_ids"]["ids"][0][0]

    # Create Tasks context with CASCADE FK to Employees
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Tasks",
            "foreign_keys": [
                {
                    "name": "employee_id",
                    "references": "Employees.id",
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create tasks for the employee
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Tasks",
            "entries": [
                {"title": "Task 1", "employee_id": emp_id},
                {"title": "Task 2", "employee_id": emp_id},
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify all data exists
    response = await _get_logs(client, project_name, context="Departments")
    assert response.json()["count"] == 1
    response = await _get_logs(client, project_name, context="Employees")
    assert response.json()["count"] == 1
    response = await _get_logs(client, project_name, context="Tasks")
    assert response.json()["count"] == 2

    # Delete the department - should CASCADE to employees and then to tasks
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify everything was CASCADE deleted
    response = await _get_logs(client, project_name, context="Departments")
    assert response.json()["count"] == 0
    response = await _get_logs(client, project_name, context="Employees")
    assert response.json()["count"] == 0
    response = await _get_logs(client, project_name, context="Tasks")
    assert response.json()["count"] == 0


@pytest.mark.anyio
async def test_mixed_actions_on_delete(client: AsyncClient):
    """Test multiple FKs with different actions (CASCADE, SET NULL, RESTRICT)."""
    project_name = "mixed-actions-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "is_versioned": True,
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create two departments
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "entries": [{"name": "Engineering"}, {"name": "Sales"}],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_ids = [ids[0] for ids in response.json()["row_ids"]["ids"]]
    dept_log_ids = response.json()["log_event_ids"]

    # Create Employees context with CASCADE FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create Contractors context with SET NULL FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Contractors",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "SET NULL",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create Projects context with RESTRICT FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Projects",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "RESTRICT",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create employee for first department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": {"name": "Alice", "department_id": dept_ids[0]},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create contractor for first department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Contractors",
            "entries": {"name": "Bob", "department_id": dept_ids[0]},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create project for second department (RESTRICT)
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Projects",
            "entries": {"title": "Project X", "department_id": dept_ids[1]},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to delete second department - should FAIL due to RESTRICT
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_ids[1], []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "RESTRICT" in response.json()["detail"]

    # Delete first department - should CASCADE employee and SET NULL contractor
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_ids[0], []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify employee was CASCADE deleted
    response = await _get_logs(client, project_name, context="Employees")
    assert response.json()["count"] == 0

    # Verify contractor still exists with NULL department_id
    response = await _get_logs(client, project_name, context="Contractors")
    assert response.json()["count"] == 1
    assert "department_id" not in response.json()["logs"][0]["entries"]

    # Verify second department and its project still exist
    response = await _get_logs(client, project_name, context="Departments")
    assert response.json()["count"] == 1
    response = await _get_logs(client, project_name, context="Projects")
    assert response.json()["count"] == 1


@pytest.mark.anyio
async def test_null_fk_values_not_affected(client: AsyncClient):
    """Test that NULL FK values are not affected by CASCADE DELETE."""
    project_name = "null-fk-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "is_versioned": True,
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "entries": {"name": "Engineering"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_id = response.json()["row_ids"]["ids"][0][0]
    dept_log_id = response.json()["log_event_ids"][0]

    # Create Employees context with CASCADE FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create employees - some with department, some without (NULL)
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": [
                {"name": "Alice", "department_id": dept_id},  # Has department
                {"name": "Bob"},  # NULL department_id
                {"name": "Charlie"},  # NULL department_id
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify all employees exist
    response = await _get_logs(client, project_name, context="Employees")
    assert response.json()["count"] == 3

    # Delete the department
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify only Alice (with department_id) was CASCADE deleted
    # Bob and Charlie (with NULL department_id) should remain
    response = await _get_logs(client, project_name, context="Employees")
    assert response.json()["count"] == 2
    remaining_names = {log["entries"]["name"] for log in response.json()["logs"]}
    assert remaining_names == {"Bob", "Charlie"}


@pytest.mark.anyio
async def test_cascade_with_no_referencing_rows(client: AsyncClient):
    """Test that CASCADE DELETE works fine when there are no referencing rows."""
    project_name = "cascade-no-refs-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "is_versioned": True,
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a department
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "entries": {"name": "Engineering"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_log_id = response.json()["log_event_ids"][0]

    # Create Employees context with CASCADE FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Don't create any employees

    # Delete the department - should succeed even with no referencing rows
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_id, []]],
            "source_type": "all",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify department was deleted
    response = await _get_logs(client, project_name, context="Departments")
    assert response.json()["count"] == 0
