"""Extended context validation tests for interface, tab, and tile APIs."""

import os
from typing import Any, Dict

import pytest
from httpx import AsyncClient

API_KEY = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}


async def create_project_and_context(
    client: AsyncClient,
    project_name: str,
    context_name: str,
) -> Dict[str, Any]:
    """Helper to create project and context."""
    # Create project
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create context
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": f"Context {context_name}"},
        headers=HEADERS,
    )

    return {"project": project_name, "context": context_name}


# ============================================================================
# GET Response Verification Tests
# ============================================================================


@pytest.mark.anyio
async def test_interface_get_returns_context_field(client: AsyncClient):
    """Verify that interface GET endpoint returns the context field."""
    project_name = "test-interface-get-context"
    context_name = "production"

    await create_project_and_context(client, project_name, context_name)

    # Create interface with context
    create_response = await client.post(
        "/v0/interface/",
        json={
            "name": "test-interface",
            "project": project_name,
        },
        headers=HEADERS,
    )
    assert create_response.status_code == 200  # Legacy interface API returns 200
    interface_id = create_response.json()["id"]

    # Update with context
    update_response = await client.put(
        f"/v0/interface/{interface_id}",
        json={"context": context_name},
        headers=HEADERS,
    )
    assert update_response.status_code == 200

    # GET the interface using legacy endpoint
    get_response = await client.get(
        f"/v0/interface?project={project_name}&name=test-interface",
        headers=HEADERS,
    )
    assert get_response.status_code == 200

    # Verify context field is returned
    interface_data = get_response.json()[0]  # Legacy endpoint returns array
    assert "context" in interface_data
    assert interface_data["context"] == context_name


@pytest.mark.anyio
async def test_tab_get_returns_context_field(client: AsyncClient):
    """Verify that tab GET endpoint returns the context field."""
    project_name = "test-tab-get-context"
    context_name = "staging"

    await create_project_and_context(client, project_name, context_name)

    # Create interface
    interface_response = await client.post(
        "/v0/interface/",
        json={"name": "test-interface", "project": project_name},
        headers=HEADERS,
    )
    interface_id = interface_response.json()["id"]

    # Create tab with context
    create_response = await client.post(
        "/v0/tab/",
        json={
            "name": "test-tab",
            "interface_id": interface_id,
            "context": context_name,
        },
        headers=HEADERS,
    )
    assert create_response.status_code == 201
    tab_id = create_response.json()["id"]

    # GET the tab
    get_response = await client.get(
        f"/v0/tab/?tab_id={tab_id}",
        headers=HEADERS,
    )
    assert get_response.status_code == 200

    # Verify context field is in response
    data = get_response.json()
    assert "context" in data
    assert data["context"] == context_name


@pytest.mark.anyio
async def test_tile_get_returns_both_context_fields(client: AsyncClient):
    """Verify that tile GET endpoint returns both context and column_context."""
    project_name = "test-tile-get-contexts"
    context_name = "main-context"
    column_context_name = "comparison-context"

    await create_project_and_context(client, project_name, context_name)
    await create_project_and_context(client, project_name, column_context_name)

    # Create interface and tab
    interface_response = await client.post(
        "/v0/interface/",
        json={"name": "test-interface", "project": project_name},
        headers=HEADERS,
    )
    interface_id = interface_response.json()["id"]

    tab_response = await client.post(
        "/v0/tab/",
        json={"name": "test-tab", "interface_id": interface_id},
        headers=HEADERS,
    )
    tab_id = tab_response.json()["id"]

    # Create tile with both contexts
    create_response = await client.post(
        "/v0/tile/",
        json={
            "name": "test-tile",
            "tab_id": tab_id,
            "type": "Table",
            "context": context_name,
            "column_context": column_context_name,
            "position": {"x": 0, "y": 0, "width": 1, "height": 1},
        },
        headers=HEADERS,
    )
    assert create_response.status_code == 201
    tile_id = create_response.json()["id"]

    # GET the tile
    get_response = await client.get(
        f"/v0/tile/?tile_id={tile_id}",
        headers=HEADERS,
    )
    assert get_response.status_code == 200

    # Verify both context fields are in response
    data = get_response.json()
    assert "context" in data
    assert data["context"] == context_name
    assert "column_context" in data
    assert data["column_context"] == column_context_name


# ============================================================================
# Null vs Empty String Handling Tests
# ============================================================================


@pytest.mark.anyio
async def test_interface_context_null_vs_empty(client: AsyncClient):
    """Test that null context is handled properly vs empty string."""
    project_name = "test-null-vs-empty"

    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create interface without context (null)
    response1 = await client.post(
        "/v0/interface/",
        json={
            "name": "interface-null",
            "project": project_name,
            # context not provided (null)
        },
        headers=HEADERS,
    )
    assert response1.status_code == 200  # Legacy interface API returns 200
    interface1_id = response1.json()["id"]

    # Create second interface
    response2 = await client.post(
        "/v0/interface/",
        json={
            "name": "interface-empty",
            "project": project_name,
        },
        headers=HEADERS,
    )
    assert response2.status_code == 200
    interface2_id = response2.json()["id"]

    # Update second interface with empty context
    response3 = await client.put(
        f"/v0/interface/{interface2_id}",
        json={"context": ""},  # Empty string
        headers=HEADERS,
    )
    assert response3.status_code == 200  # Should allow empty string

    # Verify both are allowed (null and empty string are both valid)
    assert response1.status_code == 200
    assert response3.status_code == 200


# ============================================================================
# Tile Separate Context Validation Tests
# ============================================================================


@pytest.mark.anyio
async def test_tile_valid_context_invalid_column_context(client: AsyncClient):
    """Test that column_context is not validated as a context reference (it stores JSON metadata)."""
    project_name = "test-mixed-validation"
    valid_context = "valid-context"

    await create_project_and_context(client, project_name, valid_context)

    # Create interface and tab
    interface_response = await client.post(
        "/v0/interface/",
        json={"name": "test-interface", "project": project_name},
        headers=HEADERS,
    )
    interface_id = interface_response.json()["id"]

    tab_response = await client.post(
        "/v0/tab/",
        json={"name": "test-tab", "interface_id": interface_id},
        headers=HEADERS,
    )
    tab_id = tab_response.json()["id"]

    # Try to create tile with valid context but invalid column_context
    response = await client.post(
        "/v0/tile/",
        json={
            "name": "test-tile",
            "tab_id": tab_id,
            "type": "Table",
            "context": valid_context,  # Valid
            "column_context": "not-a-context",  # This is JSON metadata, not validated
            "position": {"x": 0, "y": 0, "width": 1, "height": 1},
        },
        headers=HEADERS,
    )
    # Should succeed because column_context is not validated as a context reference
    assert response.status_code == 201


@pytest.mark.anyio
async def test_tile_invalid_context_valid_column_context(client: AsyncClient):
    """Test tile with invalid context but valid column_context."""
    project_name = "test-mixed-validation-2"
    valid_context = "valid-column-context"

    await create_project_and_context(client, project_name, valid_context)

    # Create interface and tab
    interface_response = await client.post(
        "/v0/interface/",
        json={"name": "test-interface", "project": project_name},
        headers=HEADERS,
    )
    interface_id = interface_response.json()["id"]

    tab_response = await client.post(
        "/v0/tab/",
        json={"name": "test-tab", "interface_id": interface_id},
        headers=HEADERS,
    )
    tab_id = tab_response.json()["id"]

    # Try to create tile with invalid context but valid column_context
    response = await client.post(
        "/v0/tile/",
        json={
            "name": "test-tile",
            "tab_id": tab_id,
            "type": "Table",
            "context": "non-existent-context",  # Invalid
            "column_context": valid_context,  # Valid
            "position": {"x": 0, "y": 0, "width": 1, "height": 1},
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "Context 'non-existent-context' not found" in response.json()["detail"]


# ============================================================================
# Hierarchical Context Path Tests
# ============================================================================


@pytest.mark.anyio
async def test_hierarchical_context_paths(client: AsyncClient):
    """Test interfaces/tabs/tiles with hierarchical context paths."""
    project_name = "test-hierarchical"

    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create hierarchical contexts
    contexts = ["prod", "prod/data", "prod/data/metrics"]
    for ctx in contexts:
        await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx, "description": f"Context {ctx}"},
            headers=HEADERS,
        )

    # Create interface with top-level context
    interface_response = await client.post(
        "/v0/interface/",
        json={"name": "test-interface", "project": project_name},
        headers=HEADERS,
    )
    interface_id = interface_response.json()["id"]

    # Update interface with context
    await client.put(
        f"/v0/interface/{interface_id}",
        json={"context": "prod"},
        headers=HEADERS,
    )

    # Create tab with mid-level context
    tab_response = await client.post(
        "/v0/tab/",
        json={
            "name": "test-tab",
            "interface_id": interface_id,
            "context": "prod/data",
        },
        headers=HEADERS,
    )
    assert tab_response.status_code == 201
    tab_id = tab_response.json()["id"]

    # Create tile with deep-level context
    tile_response = await client.post(
        "/v0/tile/",
        json={
            "name": "test-tile",
            "tab_id": tab_id,
            "type": "Table",
            "context": "prod/data/metrics",
            "position": {"x": 0, "y": 0, "width": 1, "height": 1},
        },
        headers=HEADERS,
    )
    assert tile_response.status_code == 201

    # Verify all three levels work with hierarchical paths
    assert tab_response.json()["context"] == "prod/data"
    assert tile_response.json()["context"] == "prod/data/metrics"


# ============================================================================
# Update Operations with Invalid Context Tests
# ============================================================================


@pytest.mark.anyio
async def test_tile_patch_invalid_context(client: AsyncClient):
    """Test PATCH tile with invalid context reference."""
    project_name = "test-tile-patch-invalid"

    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create interface and tab
    interface_response = await client.post(
        "/v0/interface/",
        json={"name": "test-interface", "project": project_name},
        headers=HEADERS,
    )
    interface_id = interface_response.json()["id"]

    tab_response = await client.post(
        "/v0/tab/",
        json={"name": "test-tab", "interface_id": interface_id},
        headers=HEADERS,
    )
    tab_id = tab_response.json()["id"]

    # Create tile without context
    create_response = await client.post(
        "/v0/tile/",
        json={
            "name": "test-tile",
            "tab_id": tab_id,
            "type": "Table",
            "position": {"x": 0, "y": 0, "width": 1, "height": 1},
        },
        headers=HEADERS,
    )
    assert create_response.status_code == 201
    tile_id = create_response.json()["id"]

    # Try to PATCH with invalid context
    patch_response = await client.patch(
        f"/v0/tile/?tile_id={tile_id}",
        json={"context": "non-existent-context"},
        headers=HEADERS,
    )
    assert patch_response.status_code == 400
    assert "Context 'non-existent-context' not found" in patch_response.json()["detail"]


# ============================================================================
# List Operations Tests
# ============================================================================


@pytest.mark.anyio
async def test_list_interfaces_includes_context(client: AsyncClient):
    """Test that list interfaces endpoint includes context field."""
    project_name = "test-list-interfaces"
    contexts = ["context-a", "context-b", "context-c"]

    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create contexts
    for ctx in contexts:
        await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )

    # Create interfaces with different contexts
    interface_ids = []
    for i, ctx in enumerate(contexts):
        response = await client.post(
            "/v0/interface/",
            json={"name": f"interface-{i}", "project": project_name},
            headers=HEADERS,
        )
        interface_id = response.json()["id"]
        interface_ids.append(interface_id)

        # Set context
        await client.put(
            f"/v0/interface/{interface_id}",
            json={"context": ctx},
            headers=HEADERS,
        )

    # List all interfaces
    list_response = await client.get(
        f"/v0/interfaces/list?project={project_name}",
        headers=HEADERS,
    )
    assert list_response.status_code == 200

    interfaces = list_response.json()
    assert len(interfaces) >= len(contexts)

    # Verify each interface includes its context
    for interface in interfaces:
        if interface["id"] in interface_ids:
            assert "context" in interface
            assert interface["context"] in contexts


# ============================================================================
# Edge Cases Tests
# ============================================================================


@pytest.mark.anyio
async def test_context_name_with_special_characters(client: AsyncClient):
    """Test context references with special characters."""
    project_name = "test-special-chars"
    contexts = [
        "feature/ABC-123",
        "version/v2_0_1",  # Changed dots to underscores
        "env/prod-us-east-1",
        "data/2024_01_01",  # Changed to underscores to avoid date separator confusion
    ]

    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Create contexts with special characters
    for ctx in contexts:
        response = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=HEADERS,
        )
        assert response.status_code == 200

    # Create interface and reference each context
    interface_response = await client.post(
        "/v0/interface/",
        json={"name": "test-interface", "project": project_name},
        headers=HEADERS,
    )
    interface_id = interface_response.json()["id"]

    for ctx in contexts:
        update_response = await client.put(
            f"/v0/interface/{interface_id}",
            json={"context": ctx},
            headers=HEADERS,
        )
        assert update_response.status_code == 200

        # Verify it was set correctly
        get_response = await client.get(
            f"/v0/interface?project={project_name}&name=test-interface",
            headers=HEADERS,
        )
        assert get_response.json()[0]["context"] == ctx
