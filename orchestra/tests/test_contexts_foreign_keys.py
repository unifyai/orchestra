"""Tests for context foreign key functionality."""

import os

import pytest
from httpx import AsyncClient

from .test_log import HEADERS, _create_log, _create_project

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


@pytest.mark.anyio
async def test_create_context_with_foreign_key(client: AsyncClient):
    """Test creating a context with a foreign key definition."""
    project_name = "fk-test-project"

    # Create project
    await _create_project(client, project_name)

    # Create referenced context (Departments)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "description": "Department master data",
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context with foreign key
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "description": "Employee data",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify foreign key is stored
    response = await client.get(
        f"/v0/project/{project_name}/contexts/Employees",
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert "foreign_keys" in data
    assert len(data["foreign_keys"]) == 1
    assert data["foreign_keys"][0]["name"] == "department_id"
    assert data["foreign_keys"][0]["references"] == "Departments.id"
    assert data["foreign_keys"][0]["on_delete"] == "CASCADE"
    assert data["foreign_keys"][0]["on_update"] == "CASCADE"


@pytest.mark.anyio
async def test_foreign_key_validation_on_insert(client: AsyncClient):
    """Test that foreign key validation works when inserting logs."""
    project_name = "fk-validation-project"

    # Create project
    await _create_project(client, project_name)

    # Create referenced context (Departments)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "description": "Department master data",
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create a department
    response = await _create_log(
        client,
        project_name,
        context="Departments",
        entries={"name": "Engineering"},
    )
    assert response.status_code == 200
    dept_row_ids = response.json()["row_ids"]
    dept_id = dept_row_ids["ids"][0][0]  # Get the auto-generated department id

    # Create context with foreign key
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "description": "Employee data",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "NO ACTION",
                    "on_update": "NO ACTION",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to insert employee with valid department_id (should succeed)
    response = await _create_log(
        client,
        project_name,
        context="Employees",
        entries={
            "name": "Alice",
            "department_id": dept_id,
        },
    )
    assert response.status_code == 200

    # Try to insert employee with invalid department_id (should fail)
    # When ALL logs fail, the existing behavior returns 400 with error detail
    response = await _create_log(
        client,
        project_name,
        context="Employees",
        entries={
            "name": "Bob",
            "department_id": 999,  # Non-existent department
        },
    )
    assert response.status_code == 400
    # The error detail should contain the foreign key violation message
    assert "Foreign key constraint violation" in response.json()["detail"]


@pytest.mark.anyio
async def test_foreign_key_nonexistent_context(client: AsyncClient):
    """Test that creating a foreign key to non-existent context fails."""
    project_name = "fk-nonexistent-project"

    # Create project
    await _create_project(client, project_name)

    # Try to create context with foreign key to non-existent context (should fail)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "description": "Employee data",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "NonExistentContext.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


@pytest.mark.anyio
async def test_foreign_key_invalid_format(client: AsyncClient):
    """Test that invalid foreign key format is rejected."""
    project_name = "fk-invalid-format-project"

    # Create project
    await _create_project(client, project_name)

    # Try to create context with invalid reference format (missing dot)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments",  # Missing column name
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 422  # Validation error
    assert "ContextName.column_name" in str(response.json())


@pytest.mark.anyio
async def test_foreign_key_null_values(client: AsyncClient):
    """Test that NULL foreign key values are allowed."""
    project_name = "fk-null-project"

    # Create project
    await _create_project(client, project_name)

    # Create referenced context
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

    # Create context with foreign key
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "SET NULL",
                    "on_update": "SET NULL",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Insert employee without department_id (NULL) - should succeed
    response = await _create_log(
        client,
        project_name,
        context="Employees",
        entries={
            "name": "Alice",
            # No department_id - implicitly NULL
        },
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_multiple_foreign_keys(client: AsyncClient):
    """Test creating a context with multiple foreign keys."""
    project_name = "fk-multiple-project"

    # Create project
    await _create_project(client, project_name)

    # Create referenced contexts
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

    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Managers",
            "unique_keys": {"id": "int"},
            "auto_counting": {"id": None},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context with multiple foreign keys
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
                {
                    "name": "manager_id",
                    "references": "Managers.id",
                    "on_delete": "SET NULL",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify both foreign keys are stored
    response = await client.get(
        f"/v0/project/{project_name}/contexts/Employees",
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["foreign_keys"]) == 2


@pytest.mark.anyio
async def test_foreign_key_duplicate_names(client: AsyncClient):
    """Test that duplicate foreign key names are rejected."""
    project_name = "fk-duplicate-project"

    # Create project
    await _create_project(client, project_name)

    # Create referenced context
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

    # Try to create context with duplicate foreign key names
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
                {
                    "name": "department_id",  # Duplicate name
                    "references": "Departments.id",
                    "on_delete": "RESTRICT",
                    "on_update": "RESTRICT",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 422  # Validation error
    assert "Duplicate" in str(response.json())


@pytest.mark.anyio
async def test_foreign_key_batch_validation(client: AsyncClient):
    """Test foreign key validation with batch log creation."""
    project_name = "fk-batch-project"

    # Create project
    await _create_project(client, project_name)

    # Create referenced context
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
            "entries": [
                {"name": "Engineering"},
                {"name": "Sales"},
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    dept_ids = [ids[0] for ids in response.json()["row_ids"]["ids"]]

    # Create context with foreign key
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Batch create employees - mix of valid and invalid department_ids
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "context": "Employees",
            "entries": [
                {"name": "Alice", "department_id": dept_ids[0]},  # Valid
                {"name": "Bob", "department_id": dept_ids[1]},  # Valid
                {"name": "Charlie", "department_id": 999},  # Invalid
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    # Should have 2 successful and 1 failed
    assert len(result["log_event_ids"]) == 2  # Only successful ones
    assert "failed" in result
    assert len(result["failed"]) == 1
    assert "Foreign key constraint violation" in result["failed"][0]["error"]
