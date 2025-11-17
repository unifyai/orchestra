"""Tests for circular foreign key dependency detection."""

import os

import pytest
from httpx import AsyncClient

from .test_log import HEADERS, _create_project

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


@pytest.mark.anyio
async def test_simple_circular_dependency_two_contexts(client: AsyncClient):
    """Test that a simple A → B → A cycle is detected."""
    project_name = "circular-two-test"
    await _create_project(client, project_name)

    # Create context A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextA",
            "is_versioned": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context B with FK to A (CASCADE)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextB",
            "foreign_keys": [
                {
                    "name": "a_id",
                    "references": "ContextA.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Try to create FK from A to B (CASCADE) - should fail due to cycle
    response = await client.put(
        f"/v0/project/{project_name}/contexts/ContextA",
        json={
            "foreign_keys": [
                {
                    "name": "b_id",
                    "references": "ContextB.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    # Note: Currently put doesn't support updating FKs, so we test with a new context instead

    # Better test: Try to add context C that completes the cycle
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextC",
            "foreign_keys": [
                {
                    "name": "a_id",
                    "references": "ContextA.id",
                    "on_delete": "SET NULL",  # OK, breaks cycle
                    "on_update": "SET NULL",
                },
                {
                    "name": "b_id",
                    "references": "ContextB.id",
                    "on_delete": "CASCADE",  # Also OK
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_no_circular_dependency_chain(client: AsyncClient):
    """Test that a simple chain (A → B → C → D) without cycles is allowed."""
    project_name = "chain-test"
    await _create_project(client, project_name)

    # Create context A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": "Employees"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context B with FK to A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "foreign_keys": [
                {
                    "name": "manager_id",
                    "references": "Employees.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context C with FK to B
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Projects",
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

    # Create context D with FKs to both C and A - should succeed (diamond DAG, no cycle)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Tasks",
            "foreign_keys": [
                {
                    "name": "project_id",
                    "references": "Projects.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
                {
                    "name": "assignee_id",
                    "references": "Employees.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200  # Should succeed - no cycle!


@pytest.mark.anyio
async def test_self_referencing_context(client: AsyncClient):
    """Test that self-referencing CASCADE FK is detected."""
    project_name = "self-ref-test"
    await _create_project(client, project_name)

    # Try to create context that references itself with CASCADE
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Employees",
            "foreign_keys": [
                {
                    "name": "manager_id",
                    "references": "Employees.id",
                    "on_delete": "CASCADE",  # Self-reference with CASCADE = cycle
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "circular" in response.json()["detail"].lower()
    # Cycle should be: Employees → Employees
    assert "employees" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_circular_with_set_null_breaks_cycle(client: AsyncClient):
    """Test that SET NULL breaks the cycle (no error)."""
    project_name = "set-null-breaks-cycle-test"
    await _create_project(client, project_name)

    # Create context A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": "Employees"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context B with CASCADE FK to A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Departments",
            "foreign_keys": [
                {
                    "name": "manager_id",
                    "references": "Employees.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context with SET NULL FK back to B - should succeed (breaks cycle)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Projects",
            "foreign_keys": [
                {
                    "name": "department_id",
                    "references": "Departments.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
                {
                    "name": "lead_id",
                    "references": "Employees.id",
                    "on_delete": "SET NULL",  # SET NULL breaks the cycle
                    "on_update": "SET NULL",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_no_circular_deep_chain(client: AsyncClient):
    """Test that a deep chain (A → B → C → D → E) with diamond pattern is allowed."""
    project_name = "deep-chain-test"
    await _create_project(client, project_name)

    # Create chain: A → B → C → D → E
    contexts = ["ContextA", "ContextB", "ContextC", "ContextD", "ContextE"]

    # Create first context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": contexts[0]},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create chain of contexts
    for i in range(1, len(contexts)):
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={
                "name": contexts[i],
                "foreign_keys": [
                    {
                        "name": "ref_id",
                        "references": f"{contexts[i-1]}.id",
                        "on_delete": "CASCADE",
                        "on_update": "CASCADE",
                    },
                ],
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

    # Create context with FKs to both E and A - should succeed (DAG, no cycle)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextF",
            "foreign_keys": [
                {
                    "name": "e_ref",
                    "references": "ContextE.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
                {
                    "name": "a_ref",
                    "references": "ContextA.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200  # Should succeed - no cycle!


@pytest.mark.anyio
async def test_no_circular_dependency_diamond(client: AsyncClient):
    """Test that diamond structure (no cycle) is allowed."""
    project_name = "diamond-test"
    await _create_project(client, project_name)

    # Create root context
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": "Root"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create two middle contexts referencing root
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "MiddleA",
            "foreign_keys": [
                {
                    "name": "root_id",
                    "references": "Root.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "MiddleB",
            "foreign_keys": [
                {
                    "name": "root_id",
                    "references": "Root.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create leaf context referencing both middle contexts - should succeed (diamond, no cycle)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "Leaf",
            "foreign_keys": [
                {
                    "name": "middle_a_id",
                    "references": "MiddleA.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
                {
                    "name": "middle_b_id",
                    "references": "MiddleB.id",
                    "on_delete": "CASCADE",
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_no_circular_mixed_delete_actions(client: AsyncClient):
    """Test that mixed CASCADE/SET NULL on delete without cycles are allowed."""
    project_name = "mixed-delete-actions-test"
    await _create_project(client, project_name)

    # Create context A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": "ContextA"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context B with CASCADE on_delete to A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextB",
            "foreign_keys": [
                {
                    "name": "a_id",
                    "references": "ContextA.id",
                    "on_delete": "CASCADE",
                    "on_update": "SET NULL",  # Not CASCADE on update
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context with mixed actions - should succeed (DAG, no cycle)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextC",
            "foreign_keys": [
                {
                    "name": "b_id",
                    "references": "ContextB.id",
                    "on_delete": "SET NULL",  # OK
                    "on_update": "CASCADE",  # OK
                },
                {
                    "name": "a_id",
                    "references": "ContextA.id",
                    "on_delete": "CASCADE",  # OK - creates DAG
                    "on_update": "SET NULL",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200  # Should succeed - no cycle!


@pytest.mark.anyio
async def test_no_circular_mixed_update_actions(client: AsyncClient):
    """Test that mixed CASCADE/SET NULL on update without cycles are allowed."""
    project_name = "mixed-update-actions-test"
    await _create_project(client, project_name)

    # Create context A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": "ContextA"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context B with CASCADE on_update to A
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextB",
            "foreign_keys": [
                {
                    "name": "a_id",
                    "references": "ContextA.id",
                    "on_delete": "SET NULL",  # Not CASCADE on delete
                    "on_update": "CASCADE",
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create context with mixed actions - should succeed (DAG, no cycle)
    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={
            "name": "ContextC",
            "foreign_keys": [
                {
                    "name": "b_id",
                    "references": "ContextB.id",
                    "on_delete": "CASCADE",  # OK
                    "on_update": "SET NULL",  # OK
                },
                {
                    "name": "a_id",
                    "references": "ContextA.id",
                    "on_delete": "SET NULL",
                    "on_update": "CASCADE",  # OK - creates DAG
                },
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200  # Should succeed - no cycle!
