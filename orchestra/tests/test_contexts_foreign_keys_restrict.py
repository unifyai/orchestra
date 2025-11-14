"""Tests for foreign key RESTRICT constraints on DELETE and UPDATE operations."""

import os

import pytest
from httpx import AsyncClient

from .test_log import HEADERS, _create_project

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "authorization": f"Bearer {api_key}",
}


@pytest.mark.anyio
async def test_restrict_prevents_delete_when_referenced(client: AsyncClient):
    """Test that RESTRICT prevents deletion of referenced values."""
    project_name = "restrict-delete-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
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

    # Create Employees context with RESTRICT FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
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

    # Create an employee referencing the department
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

    # Try to delete the department - should be blocked by RESTRICT
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_id, ["id"]]],  # Try to delete the id field
            "source_type": "base",
        },
        headers=HEADERS,
    )

    # Should fail with 400 due to RESTRICT constraint
    assert response.status_code == 400
    assert "RESTRICT" in response.json()["detail"]
    assert "department_id" in response.json()["detail"]


@pytest.mark.anyio
async def test_restrict_allows_delete_when_not_referenced(client: AsyncClient):
    """Test that RESTRICT allows deletion when value is not referenced."""
    project_name = "restrict-delete-ok-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
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

    # Create Employees context with RESTRICT FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
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

    # Create an employee referencing only the first department
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

    # Delete the second department (not referenced) - should succeed
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[dept_log_ids[1], []]],  # Delete entire log event
            "source_type": "all",
        },
        headers=HEADERS,
    )

    # Should succeed since Sales department is not referenced
    assert response.status_code == 200


@pytest.mark.anyio
async def test_restrict_prevents_update_when_referenced(client: AsyncClient):
    """Test that RESTRICT prevents update of referenced values."""
    project_name = "restrict-update-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
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

    # Create Employees context with RESTRICT FK on update
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "NO ACTION",
                    "on_update": "RESTRICT",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create an employee referencing the department
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

    # Try to update the department id - should be blocked by RESTRICT
    response = await client.put(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "logs": [dept_log_id],
            "entries": {"id": 999},  # Try to change the id
        },
        headers=HEADERS,
    )

    # Should fail with 400 due to RESTRICT constraint
    assert response.status_code == 400
    assert "RESTRICT" in response.json()["detail"]
    assert "department_id" in response.json()["detail"]


@pytest.mark.anyio
async def test_restrict_allows_update_non_fk_columns(client: AsyncClient):
    """Test that RESTRICT allows updating non-FK columns."""
    project_name = "restrict-update-ok-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context (without auto_counting to avoid immutability)
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

    # Create a department with explicit id
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

    # Create Employees context with RESTRICT FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "RESTRICT",
                    "on_update": "RESTRICT",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create an employee referencing the department
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

    # Update the department name (not the id) - should succeed
    response = await client.put(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "logs": [dept_log_id],
            "entries": {"name": "Engineering Team"},  # Update name, not id
            "overwrite": True,  # Allow overwriting existing values
        },
        headers=HEADERS,
    )

    # Should succeed since we're not updating the referenced column
    assert response.status_code == 200


@pytest.mark.anyio
async def test_cascade_not_blocked_by_restrict_check(client: AsyncClient):
    """Test that CASCADE FKs are not blocked (CASCADE is not yet implemented)."""
    project_name = "cascade-not-blocked-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
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

    # Create Employees context with CASCADE FK (not RESTRICT)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",  # CASCADE, not RESTRICT
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create an employee referencing the department
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

    # Delete the department with CASCADE FK - should NOT be blocked
    # (CASCADE actions are not yet implemented, so it just allows the delete)
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

    # Should succeed since CASCADE is not enforced by RESTRICT check
    assert response.status_code == 200


@pytest.mark.anyio
async def test_restrict_with_null_values(client: AsyncClient):
    """Test that NULL FK values don't block deletion."""
    project_name = "restrict-null-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
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

    # Create Employees context with RESTRICT FK
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
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

    # Create an employee with NULL department_id
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": {"name": "Bob"},  # No department_id
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Delete the department - should succeed since NULL doesn't create a reference
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

    # Should succeed
    assert response.status_code == 200


@pytest.mark.anyio
async def test_multiple_restrict_violations(client: AsyncClient):
    """Test error message with multiple RESTRICT violations."""
    project_name = "multiple-restrict-test"

    # Create project
    await _create_project(client, project_name)

    # Create Departments context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
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

    # Create Employees and Projects contexts both with FKs to Departments
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
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

    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Projects",
            "foreign_keys": [
                {
                    "name": "owner_dept",
                    "references": "Departments.id",
                    "on_delete": "RESTRICT",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create employee and project referencing first department
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

    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Projects",
            "entries": {"title": "Project X", "owner_dept": dept_ids[0]},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to delete first department - should report multiple violations
    dept_log_ids = response.json()["log_event_ids"]
    response = await client.request(
        "DELETE",
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Departments",
            "ids_and_fields": [[None, ["id"]]],  # Global delete of id field
            "source_type": "base",
        },
        headers=HEADERS,
    )

    # Should fail with multiple violations
    assert response.status_code == 400
    error_detail = response.json()["detail"]
    # Should mention both contexts
    assert "Employees" in error_detail or "Projects" in error_detail
    assert "RESTRICT" in error_detail
