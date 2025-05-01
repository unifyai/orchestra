import os
import uuid

import pytest
from httpx import AsyncClient

# Common headers and data
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

# Test project and resource names
TEST_PROJECT = "test-interface-project"
TEST_INTERFACE = "test-interface"


# Helper functions for project and interface creation
async def _create_project(client: AsyncClient, project_name=TEST_PROJECT):
    """Create a test project"""
    response = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    return response


async def _delete_project(client: AsyncClient, project_name=TEST_PROJECT):
    """Delete a test project"""
    response = await client.delete(f"/v0/project/{project_name}", headers=HEADERS)
    return response


# Interface helpers
async def _create_test_interface(
    client: AsyncClient,
    name=TEST_INTERFACE,
    project=TEST_PROJECT,
    color="#FF0000",
):
    """Create a test interface"""
    response = await client.post(
        "/v0/interfaces/",
        headers=HEADERS,
        json={"name": name, "project": project, "color": color},
    )
    return response


async def _get_interface(
    client: AsyncClient,
    interface_id=None,
    project=None,
    name=None,
):
    """
    Get interface by ID or by project+name

    If interface_id is provided, gets a single interface by ID
    If project and name are provided, gets a single interface by project and name
    """
    if interface_id:
        return await client.get(
            f"/v0/interfaces/?interface_id={interface_id}",
            headers=HEADERS,
        )
    elif project and name:
        return await client.get(
            f"/v0/interfaces/?project={project}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either interface_id or project+name")


async def _list_interfaces(client: AsyncClient, project=None):
    """List interfaces for a project"""
    if project:
        return await client.get(
            f"/v0/interfaces/list?project={project}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide project")


async def _update_interface(
    client: AsyncClient,
    interface_id=None,
    project=None,
    name=None,
    update_data=None,
):
    """Update interface by ID or by project+name"""
    if update_data is None:
        update_data = {}

    if interface_id:
        return await client.put(
            f"/v0/interfaces/?interface_id={interface_id}",
            headers=HEADERS,
            json=update_data,
        )
    elif project and name:
        return await client.put(
            f"/v0/interfaces/?project={project}&name={name}",
            headers=HEADERS,
            json=update_data,
        )
    else:
        raise ValueError("Must provide either interface_id or project+name")


async def _delete_interface(
    client: AsyncClient,
    interface_id=None,
    project=None,
    name=None,
):
    """Delete interface by ID or by project+name"""
    if interface_id:
        return await client.delete(
            f"/v0/interfaces/?interface_id={interface_id}",
            headers=HEADERS,
        )
    elif project and name:
        return await client.delete(
            f"/v0/interfaces/?project={project}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either interface_id or project+name")


async def _create_interface_checkpoint(
    client: AsyncClient,
    interface_id=None,
    project=None,
    name=None,
):
    """Create a checkpoint for an interface"""
    if interface_id:
        return await client.post(
            f"/v0/interfaces/checkpoint?interface_id={interface_id}",
            headers=HEADERS,
        )
    elif project and name:
        return await client.post(
            f"/v0/interfaces/checkpoint?project={project}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either interface_id or project+name")


async def _get_interface_checkpoint(
    client: AsyncClient,
    interface_id=None,
    project=None,
    name=None,
):
    """Get the latest checkpoint for an interface"""
    if interface_id:
        return await client.get(
            f"/v0/interfaces/checkpoint?interface_id={interface_id}",
            headers=HEADERS,
        )
    elif project and name:
        return await client.get(
            f"/v0/interfaces/checkpoint?project={project}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either interface_id or project+name")


# Test fixtures
@pytest.fixture(autouse=True)
async def setup_test_project(client: AsyncClient):
    """Setup and teardown for a test project"""
    # Setup
    await _create_project(client)

    # Run test
    yield

    # Teardown
    await _delete_project(client)


# Interface Tests
@pytest.mark.anyio
async def test_create_interface(client: AsyncClient):
    """Test creating an interface"""
    response = await _create_test_interface(client)
    assert response.status_code == 201

    data = response.json()
    # Check mandatory fields from InterfaceSchema
    assert data["name"] == TEST_INTERFACE
    assert data["color"] == "#FF0000"
    assert data["is_checkpoint"] is False
    assert "id" in data
    assert "created_at" in data
    # Verify schema structure for nested objects
    assert "tabs" in data
    assert isinstance(data["tabs"], list)
    assert data["active_tab_id"] is None or isinstance(data["active_tab_id"], str)


@pytest.mark.anyio
async def test_get_interface_by_id(client: AsyncClient):
    """Test getting an interface by ID"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    # Get the interface by ID
    response = await _get_interface(client, interface_id=interface_id)
    assert response.status_code == 200

    data = response.json()
    # Verify the correct interface was retrieved
    assert data["id"] == interface_id
    assert data["name"] == TEST_INTERFACE
    assert data["is_checkpoint"] is False
    # Verify schema structure
    assert "tabs" in data
    assert isinstance(data["tabs"], list)
    assert "created_at" in data
    assert data["active_tab_id"] is None or isinstance(data["active_tab_id"], str)


@pytest.mark.anyio
async def test_get_interface_by_project_and_name(client: AsyncClient):
    """Test getting an interface by project and name"""
    # Create an interface
    await _create_test_interface(client)

    # Get the interface by project and name
    response = await _get_interface(client, project=TEST_PROJECT, name=TEST_INTERFACE)
    assert response.status_code == 200

    data = response.json()
    # Verify the correct interface was retrieved
    assert data["name"] == TEST_INTERFACE
    assert data["is_checkpoint"] is False
    # Verify schema structure
    assert "id" in data
    assert "tabs" in data
    assert isinstance(data["tabs"], list)
    assert "created_at" in data
    assert data["active_tab_id"] is None or isinstance(data["active_tab_id"], str)


@pytest.mark.anyio
async def test_list_interfaces(client: AsyncClient):
    """Test listing interfaces for a project"""
    # Create multiple interfaces
    await _create_test_interface(client, name="list-interface-1")
    await _create_test_interface(client, name="list-interface-2")

    # List interfaces by project
    response = await _list_interfaces(client, project=TEST_PROJECT)
    assert response.status_code == 200

    data = response.json()
    # Verify all created interfaces are in the list
    assert len(data) >= 2
    interface_names = [interface["name"] for interface in data]
    assert "list-interface-1" in interface_names
    assert "list-interface-2" in interface_names

    # Verify each interface in the list has the correct schema
    for interface in data:
        assert "id" in interface
        assert "name" in interface
        assert "project_id" in interface
        assert "tabs" in interface
        assert isinstance(interface["tabs"], list)
        assert "is_checkpoint" in interface
        assert "created_at" in interface
        assert "active_tab_id" in interface  # May be null, but field should exist
        assert interface["active_tab_id"] is None or isinstance(
            interface["active_tab_id"],
            str,
        )


@pytest.mark.anyio
async def test_update_interface_by_id(client: AsyncClient):
    """Test updating an interface by ID"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    original_created_at = interface_response.json()["created_at"]

    # Update the interface by ID
    new_name = "updated-interface"
    new_color = "#00FF00"
    update_data = {"name": new_name, "color": new_color}
    response = await _update_interface(
        client,
        interface_id=interface_id,
        update_data=update_data,
    )
    assert response.status_code == 200

    data = response.json()
    # Verify the update was successful
    assert data["id"] == interface_id  # ID should not change
    assert data["name"] == new_name  # Name should be updated
    assert data["color"] == new_color  # Color should be updated
    assert data["is_checkpoint"] is False  # Should still not be a checkpoint
    assert (
        data["created_at"] == original_created_at
    )  # Created timestamp should not change

    # Verify schema structure
    assert "tabs" in data
    assert isinstance(data["tabs"], list)
    assert "updated_at" in data  # Should have an updated timestamp
    assert data["active_tab_id"] is None or isinstance(data["active_tab_id"], str)

    # Verify the update persists by getting the interface again
    get_response = await _get_interface(client, interface_id=interface_id)
    get_data = get_response.json()
    assert get_data["name"] == new_name
    assert get_data["color"] == new_color


@pytest.mark.anyio
async def test_update_interface_by_project_and_name(client: AsyncClient):
    """Test updating an interface by project and name"""
    # Create an interface
    create_response = await _create_test_interface(client)
    original_id = create_response.json()["id"]
    original_created_at = create_response.json()["created_at"]

    # Update the interface by project and name
    new_color = "#00FF00"
    update_data = {"color": new_color}
    response = await _update_interface(
        client,
        project=TEST_PROJECT,
        name=TEST_INTERFACE,
        update_data=update_data,
    )
    assert response.status_code == 200

    data = response.json()
    # Verify the update was successful
    assert data["id"] == original_id  # ID should not change
    assert data["name"] == TEST_INTERFACE  # Name should remain the same
    assert data["color"] == new_color  # Color should be updated
    assert data["is_checkpoint"] is False  # Should still not be a checkpoint
    assert (
        data["created_at"] == original_created_at
    )  # Created timestamp should not change

    # Verify schema structure
    assert "tabs" in data
    assert isinstance(data["tabs"], list)
    assert "updated_at" in data  # Should have an updated timestamp
    assert data["active_tab_id"] is None or isinstance(data["active_tab_id"], str)

    # Verify the update persists by getting the interface again
    get_response = await _get_interface(
        client,
        project=TEST_PROJECT,
        name=TEST_INTERFACE,
    )
    get_data = get_response.json()
    assert get_data["color"] == new_color


@pytest.mark.anyio
async def test_delete_interface_by_id(client: AsyncClient):
    """Test deleting an interface by ID"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Delete the interface by ID
    response = await _delete_interface(client, interface_id=interface_id)
    assert response.status_code == 204

    # Verify interface is deleted
    get_response = await _get_interface(client, interface_id=interface_id)
    assert get_response.status_code == 404


@pytest.mark.anyio
async def test_delete_interface_by_project_and_name(client: AsyncClient):
    """Test deleting an interface by project and name"""
    # Create an interface
    await _create_test_interface(client)

    # Delete the interface by project and name
    response = await _delete_interface(
        client,
        project=TEST_PROJECT,
        name=TEST_INTERFACE,
    )
    assert response.status_code == 204

    # Verify interface is deleted
    get_response = await _get_interface(
        client,
        project=TEST_PROJECT,
        name=TEST_INTERFACE,
    )
    assert get_response.status_code == 404


@pytest.mark.anyio
async def test_interface_checkpoint_by_id(client: AsyncClient):
    """Test creating interface checkpoints by ID"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    original_created_at = interface_response.json()["created_at"]

    # Create a checkpoint by ID
    response = await _create_interface_checkpoint(client, interface_id=interface_id)
    assert response.status_code == 200

    checkpoint_data = response.json()
    # Verify checkpoint was created correctly
    assert checkpoint_data["is_checkpoint"] is True
    assert checkpoint_data["name"] == TEST_INTERFACE
    assert (
        checkpoint_data["id"] != interface_id
    )  # Should be a new ID for the checkpoint
    assert checkpoint_data["color"] == "#FF0000"  # Should copy original color
    assert (
        checkpoint_data["created_at"] != original_created_at
    )  # Should have a new timestamp

    # Verify schema structure
    assert "tabs" in checkpoint_data
    assert isinstance(checkpoint_data["tabs"], list)
    assert "updated_at" in checkpoint_data
    assert checkpoint_data["active_tab_id"] is None or isinstance(
        checkpoint_data["active_tab_id"],
        str,
    )


@pytest.mark.anyio
async def test_interface_checkpoint_by_project_and_name(client: AsyncClient):
    """Test creating interface checkpoints by project and name"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    original_created_at = interface_response.json()["created_at"]

    # Create a checkpoint by project and name
    response = await _create_interface_checkpoint(
        client,
        project=TEST_PROJECT,
        name=TEST_INTERFACE,
    )
    assert response.status_code == 200

    checkpoint_data = response.json()
    # Verify checkpoint was created correctly
    assert checkpoint_data["is_checkpoint"] is True
    assert checkpoint_data["name"] == TEST_INTERFACE
    assert (
        checkpoint_data["id"] != interface_id
    )  # Should be a new ID for the checkpoint
    assert checkpoint_data["color"] == "#FF0000"  # Should copy original color
    assert (
        checkpoint_data["created_at"] != original_created_at
    )  # Should have a new timestamp

    # Verify schema structure
    assert "tabs" in checkpoint_data
    assert isinstance(checkpoint_data["tabs"], list)
    assert "updated_at" in checkpoint_data
    assert checkpoint_data["active_tab_id"] is None or isinstance(
        checkpoint_data["active_tab_id"],
        str,
    )


@pytest.mark.anyio
async def test_get_interface_checkpoint(client: AsyncClient):
    """Test retrieving interface checkpoints"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    original_created_at = interface_response.json()["created_at"]

    # Create a checkpoint
    checkpoint_response = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    checkpoint_id = checkpoint_response.json()["id"]

    # Get the checkpoint
    response = await _get_interface_checkpoint(client, interface_id=checkpoint_id)
    assert response.status_code == 200

    data = response.json()
    # Verify correct checkpoint was retrieved
    assert data["is_checkpoint"] is True
    assert data["name"] == TEST_INTERFACE
    assert data["id"] == checkpoint_id  # Should match the ID from checkpoint creation
    assert (
        data["created_at"] != original_created_at
    )  # Should have a different timestamp

    # Verify schema structure
    assert "tabs" in data
    assert isinstance(data["tabs"], list)
    assert "updated_at" in data
    assert data["active_tab_id"] is None or isinstance(data["active_tab_id"], str)


@pytest.mark.anyio
async def test_create_duplicate_interface(client: AsyncClient):
    """Test creating a duplicate interface (should fail)"""
    # Create an interface
    await _create_test_interface(client)

    # Try to create another interface with the same name
    response = await _create_test_interface(client)
    assert response.status_code == 409

    # Verify error response has helpful message
    error_data = response.json()
    assert "detail" in error_data
    assert "already exists" in error_data["detail"].lower()


@pytest.mark.anyio
async def test_interface_with_nonexistent_project(client: AsyncClient):
    """Test creating an interface with a non-existent project (should fail)"""
    non_existent_project = str(uuid.uuid4())  # random ID
    response = await _create_test_interface(client, project=non_existent_project)
    assert response.status_code == 404

    # Verify error response has helpful message
    error_data = response.json()
    assert "detail" in error_data
    assert "not found" in error_data["detail"].lower()


@pytest.mark.anyio
async def test_error_responses(client: AsyncClient):
    """Test error responses for invalid requests"""
    # Get with invalid ID
    get_response = await _get_interface(client, interface_id="invalid-id")
    assert get_response.status_code == 404 or get_response.status_code == 422
    error_data = get_response.json()
    assert "detail" in error_data

    # Update with invalid data
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    update_response = await _update_interface(
        client,
        interface_id=interface_id,
        update_data={"invalid_field": "value"},
    )
    assert update_response.status_code == 422  # Should be 422 for validation error
    error_data = update_response.json()
    assert "detail" in error_data
    assert "invalid_field" in str(
        error_data["detail"],
    )  # Error should mention the invalid field


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_existing_checkpoint(
    client: AsyncClient,
):
    """Test creating a checkpoint when one already exists (should update existing checkpoint)"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    assert interface_response.status_code == 201
    interface_id = interface_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert first_checkpoint.status_code == 200
    first_checkpoint_id = first_checkpoint.json()["id"]

    # Update the original interface
    update_response = await _update_interface(
        client,
        interface_id=interface_id,
        update_data={"color": "#00FF00"},
    )
    assert update_response.status_code == 200

    # Create second checkpoint (should update existing one)
    second_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert second_checkpoint.status_code == 200
    second_checkpoint_id = second_checkpoint.json()["id"]

    # Verify it's the same checkpoint (same ID)
    assert first_checkpoint_id == second_checkpoint_id
    # Verify it has the updated color
    assert second_checkpoint.json()["color"] == "#00FF00"

    # Clean up by deleting the interface
    await _delete_interface(client, interface_id=interface_id)


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_tabs(client: AsyncClient):
    """Test creating a checkpoint for an interface with tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    assert interface_response.status_code == 201
    interface_id = interface_response.json()["id"]

    # Create a tab in the interface
    tab_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "test-tab",
            "visible": True,
            "order": 1,
        },
    )
    assert tab_response.status_code == 201

    # Create a checkpoint
    checkpoint_response = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert checkpoint_response.status_code == 200

    # Verify the checkpoint has the tab
    checkpoint_data = checkpoint_response.json()
    assert len(checkpoint_data["tabs"]) == 1
    assert checkpoint_data["tabs"][0]["name"] == "test-tab"
    assert checkpoint_data["tabs"][0]["visible"] is True
    assert checkpoint_data["tabs"][0]["order"] == 1

    # Clean up by deleting the interface
    await _delete_interface(client, interface_id=interface_id)


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_multiple_tabs(client: AsyncClient):
    """Test creating a checkpoint for an interface with multiple tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    assert interface_response.status_code == 201
    interface_id = interface_response.json()["id"]

    # Create multiple tabs
    tab_names = ["tab1", "tab2", "tab3"]
    for i, name in enumerate(tab_names):
        tab_response = await client.post(
            "/v0/tab/",
            headers=HEADERS,
            json={
                "interface_id": interface_id,
                "name": name,
                "visible": True,
                "order": i,
            },
        )
        assert tab_response.status_code == 201

    # Create a checkpoint
    checkpoint_response = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert checkpoint_response.status_code == 200

    # Verify all tabs are in the checkpoint
    checkpoint_data = checkpoint_response.json()
    assert len(checkpoint_data["tabs"]) == len(tab_names)
    checkpoint_tab_names = [tab["name"] for tab in checkpoint_data["tabs"]]
    assert set(checkpoint_tab_names) == set(tab_names)

    # Clean up by deleting the interface
    await _delete_interface(client, interface_id=interface_id)


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_invisible_tabs(client: AsyncClient):
    """Test creating a checkpoint for an interface with invisible tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    assert interface_response.status_code == 201
    interface_id = interface_response.json()["id"]

    # Create a tab with visible=False
    tab_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "invisible-tab",
            "visible": False,
            "order": 1,
        },
    )
    assert tab_response.status_code == 201

    # Create a checkpoint
    checkpoint_response = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert checkpoint_response.status_code == 200

    # Verify the checkpoint preserves the tab's visibility
    checkpoint_data = checkpoint_response.json()
    assert len(checkpoint_data["tabs"]) == 1
    assert checkpoint_data["tabs"][0]["visible"] is False

    # Clean up by deleting the interface
    await _delete_interface(client, interface_id=interface_id)


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_active_tab(client: AsyncClient):
    """Test creating a checkpoint for an interface with an active tab"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    assert interface_response.status_code == 201
    interface_id = interface_response.json()["id"]

    # Create a tab
    tab_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "active-tab",
            "visible": True,
            "order": 1,
        },
    )
    assert tab_response.status_code == 201
    tab_id = tab_response.json()["id"]

    # Set the tab as active
    update_response = await _update_interface(
        client,
        interface_id=interface_id,
        update_data={"active_tab_id": tab_id},
    )
    assert update_response.status_code == 200

    # Create a checkpoint
    checkpoint_response = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert checkpoint_response.status_code == 200

    # Verify the checkpoint preserves the active tab
    checkpoint_data = checkpoint_response.json()
    assert checkpoint_data["active_tab_id"] == tab_id

    # Clean up by deleting the interface
    await _delete_interface(client, interface_id=interface_id)


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_tab_updates(client: AsyncClient):
    """Test creating a checkpoint after updating tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create a tab
    tab_response = await client.post(
        f"/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "test-tab",
            "visible": True,
            "order": 1,
        },
    )
    assert tab_response.status_code == 201
    tab_id = tab_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert first_checkpoint.status_code == 200

    # Update the tab
    update_tab_response = await client.put(
        f"/v0/tab/?tab_id={tab_id}",
        headers=HEADERS,
        json={"visible": False, "order": 2},
    )
    assert update_tab_response.status_code == 200

    # Create second checkpoint
    second_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has the updated tab properties
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tabs"]) == 1
    assert checkpoint_data["tabs"][0]["visible"] is False
    assert checkpoint_data["tabs"][0]["order"] == 2


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_deleted_tabs(client: AsyncClient):
    """Test creating a checkpoint after deleting tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create a tab
    tab_response = await client.post(
        f"/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "test-tab",
            "visible": True,
            "order": 1,
        },
    )
    assert tab_response.status_code == 201
    tab_id = tab_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert first_checkpoint.status_code == 200

    # Delete the tab
    delete_tab_response = await client.delete(
        f"/v0/tab/?tab_id={tab_id}",
        headers=HEADERS,
    )
    assert delete_tab_response.status_code == 204

    # Create second checkpoint
    second_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has no tabs
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tabs"]) == 0


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_added_tabs(client: AsyncClient):
    """Test creating a checkpoint after adding new tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create first checkpoint (no tabs)
    first_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert first_checkpoint.status_code == 200
    assert len(first_checkpoint.json()["tabs"]) == 0

    # Add a tab
    tab_response = await client.post(
        f"/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "new-tab",
            "visible": True,
            "order": 1,
        },
    )
    assert tab_response.status_code == 201

    # Create second checkpoint
    second_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has the new tab
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tabs"]) == 1
    assert checkpoint_data["tabs"][0]["name"] == "new-tab"


@pytest.mark.anyio
async def test_create_interface_checkpoint_with_renamed_tabs(client: AsyncClient):
    """Test creating a checkpoint after renaming tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create a tab
    tab_response = await client.post(
        f"/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "old-name",
            "visible": True,
            "order": 1,
        },
    )
    assert tab_response.status_code == 201
    tab_id = tab_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert first_checkpoint.status_code == 200

    # Rename the tab
    update_tab_response = await client.put(
        f"/v0/tab/?tab_id={tab_id}",
        headers=HEADERS,
        json={"name": "new-name"},
    )
    assert update_tab_response.status_code == 200

    # Create second checkpoint
    second_checkpoint = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has the renamed tab
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tabs"]) == 1
    assert checkpoint_data["tabs"][0]["name"] == "new-name"
