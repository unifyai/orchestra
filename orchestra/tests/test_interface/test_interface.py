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
    assert response.status_code == 200

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
    assert response.status_code == 200

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
    response = await _get_interface_checkpoint(client, interface_id=interface_id)
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
    assert delete_tab_response.status_code == 200

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


@pytest.mark.anyio
async def test_restore_interface_checkpoint(client: AsyncClient):
    """Ensure that an interface checkpoint remains unchanged after updates to the active interface."""
    # 1. Create an interface with a known color
    create_resp = await _create_test_interface(client, color="#FF0000")
    assert create_resp.status_code == 201
    interface_id = create_resp.json()["id"]

    # 2. Create a checkpoint for this interface
    checkpoint_resp = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert checkpoint_resp.status_code == 200
    checkpoint_id = checkpoint_resp.json()["id"]

    # Sanity check that checkpoint captured the original color
    assert checkpoint_resp.json()["color"] == "#FF0000"
    assert checkpoint_resp.json()["is_checkpoint"] is True

    # 3. Update the active interface (change color and name)
    update_payload = {"color": "#00FF00", "name": "updated-interface-name"}
    update_resp = await _update_interface(
        client,
        interface_id=interface_id,
        update_data=update_payload,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["color"] == "#00FF00"
    assert update_resp.json()["name"] == "updated-interface-name"

    # 4. Fetch the checkpoint again and verify it is unchanged
    checkpoint_get = await _get_interface_checkpoint(client, interface_id=interface_id)
    assert checkpoint_get.status_code == 200
    checkpoint_data = checkpoint_get.json()

    # The checkpoint should still reflect the original values
    assert checkpoint_data["color"] == "#FF0000"
    assert checkpoint_data["name"] == TEST_INTERFACE  # original name
    assert checkpoint_data["is_checkpoint"] is True

    # Active interface should reflect updated values
    active_get = await _get_interface(client, interface_id=interface_id)
    assert active_get.status_code == 200
    active_data = active_get.json()
    assert active_data["color"] == "#00FF00"
    assert active_data["name"] == "updated-interface-name"


@pytest.mark.anyio
async def test_export_interface_template_with_valid_schema(client: AsyncClient):
    """Test exporting an interface template with valid schema"""
    # Create an interface with tabs and tiles
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create tabs with different properties
    tab1_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "data_tab",
            "visible": True,
            "active": True,
            "order": 0,
            "color": "#FF0000",
        },
    )
    assert tab1_response.status_code == 201
    tab1_id = tab1_response.json()["id"]

    tab2_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "viz_tab",
            "visible": True,
            "active": False,
            "order": 1,
            "color": "#00FF00",
        },
    )
    assert tab2_response.status_code == 201
    tab2_id = tab2_response.json()["id"]

    # Create tiles in tabs
    from orchestra.tests.test_tile import (
        _create_test_plot_tile,
        _create_test_table_tile,
    )

    await _create_test_table_tile(
        client,
        tab1_id,
        name="summary_table",
        table_type="advanced",
    )
    await _create_test_plot_tile(
        client,
        tab1_id,
        name="trend_chart",
        plot_type="line",
    )
    await _create_test_plot_tile(
        client,
        tab2_id,
        name="scatter_plot",
        plot_type="scatter",
    )

    # Export interface template
    export_request = {
        "interface_id": interface_id,
        "include_metadata": True,
        "description": "Test interface template",
        "tags": ["test", "interface"],
        "template_name": "Test Interface Template",
    }

    response = await client.post(
        "/v0/interfaces/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    # Verify template structure
    assert "template" in data
    assert "metadata" in data
    assert "export_stats" in data

    template = data["template"]
    assert template["name"] == TEST_INTERFACE
    assert template["template_version"] == "1.0"
    assert template["description"] == "Test interface template"
    assert "test" in template["tags"]
    assert len(template["tabs"]) == 2

    # Verify tabs are exported correctly
    tab_names = [tab["name"] for tab in template["tabs"]]
    assert "data_tab" in tab_names
    assert "viz_tab" in tab_names

    # Verify tiles are exported
    total_tiles = sum(len(tab["tiles"]) for tab in template["tabs"])
    assert total_tiles == 3

    # Verify export stats
    stats = data["export_stats"]
    assert stats["tabs"] == 2
    assert stats["tiles"] == 3


@pytest.mark.anyio
async def test_export_interface_template_with_valid_schema_by_project_and_name(
    client: AsyncClient,
):
    """Test exporting interface template with valid schema using project and name"""
    await _create_test_interface(client)

    export_request = {
        "project": TEST_PROJECT,
        "interface_name": TEST_INTERFACE,
        "include_metadata": True,
        "description": "Export by project and name",
    }

    response = await client.post(
        "/v0/interfaces/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    template = data["template"]
    assert template["name"] == TEST_INTERFACE
    assert template["description"] == "Export by project and name"


@pytest.mark.anyio
async def test_export_interface_template_with_valid_schema_checkpoint(
    client: AsyncClient,
):
    """Test exporting interface template with valid schema from checkpoint"""
    # Create interface and add content
    interface_response = await _create_test_interface(client, color="#FF0000")
    interface_id = interface_response.json()["id"]

    # Create checkpoint
    checkpoint_response = await _create_interface_checkpoint(
        client,
        interface_id=interface_id,
    )
    assert checkpoint_response.status_code == 200

    # Update original interface after checkpoint
    await _update_interface(
        client,
        interface_id=interface_id,
        update_data={"color": "#00FF00"},
    )

    # Export from checkpoint
    checkpoint_interface_id = checkpoint_response.json()["id"]
    export_request = {
        "interface_id": checkpoint_interface_id,
        "checkpoint": True,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/interfaces/export_template",
        json=export_request,
        headers=HEADERS,
    )

    print(response.json())

    assert response.status_code == 200
    data = response.json()

    # Should export checkpoint version (original color)
    template = data["template"]
    assert template["color"] == "#FF0000"  # Original color from checkpoint


@pytest.mark.anyio
async def test_export_interface_template_with_valid_schema_empty_interface(
    client: AsyncClient,
):
    """Test exporting interface template with valid schema from empty interface"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    export_request = {
        "interface_id": interface_id,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/interfaces/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    template = data["template"]
    assert template["name"] == TEST_INTERFACE
    assert len(template["tabs"]) == 0

    stats = data["export_stats"]
    assert stats["tabs"] == 0
    assert stats["tiles"] == 0


@pytest.mark.anyio
async def test_import_interface_template_with_valid_schema(client: AsyncClient):
    """Test importing an interface template with valid schema"""
    # Create a valid template
    template = {
        "name": "imported_interface",
        "color": "#FF00FF",
        "tabs": [
            {
                "name": "imported_tab",
                "visible": True,
                "active": True,
                "order": 0,
                "color": "#00FFFF",
                "tiles": [
                    {
                        "name": "imported_table",
                        "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                        "type": "Table",
                        "visible": True,
                        "table_tile": {
                            "table_type": "advanced",
                            "page_number": "1",
                        },
                    },
                    {
                        "name": "imported_plot",
                        "position": {"x": 6, "y": 0, "width": 6, "height": 4},
                        "type": "Plot",
                        "visible": True,
                        "plot_tile": {
                            "plot_type": "scatter",
                            "x_axis": "x",
                            "y_axis": "y",
                        },
                    },
                ],
            },
        ],
        "template_version": "1.0",
        "description": "Test import template",
        "tags": ["imported", "test"],
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "validate_first": False,  # Skip validation for v0
        "auto_sanitize": False,
        "overwrite_existing": False,
    }

    response = await client.post(
        "/v0/interfaces/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["interfaces"] == 1
    assert data["import_stats"]["tabs"] == 1
    assert data["import_stats"]["tiles"] == 2

    # Verify the interface was created
    created_interface_id = data["created_ids"]["interface_id"]
    get_response = await _get_interface(client, interface_id=created_interface_id)
    assert get_response.status_code == 200

    interface_data = get_response.json()
    assert interface_data["name"] == "imported_interface"
    assert interface_data["color"] == "#FF00FF"
    assert len(interface_data["tabs"]) == 1

    # Verify tab was created correctly
    tab = interface_data["tabs"][0]
    assert tab["name"] == "imported_tab"
    assert tab["color"] == "#00FFFF"
    assert tab["active"] is True
    assert len(tab["tiles"]) == 2

    # Verify tiles were created correctly
    tile_names = [tile["name"] for tile in tab["tiles"]]
    assert "imported_table" in tile_names
    assert "imported_plot" in tile_names


@pytest.mark.anyio
async def test_import_interface_template_with_valid_schema_new_name(
    client: AsyncClient,
):
    """Test importing interface template with valid schema and new name override"""
    template = {
        "name": "original_name",
        "tabs": [
            {
                "name": "test_tab",
                "tiles": [],
            },
        ],
        "template_version": "1.0",
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "new_interface_name": "overridden_name",
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/interfaces/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    # Verify interface was created with new name
    created_interface_id = data["created_ids"]["interface_id"]
    get_response = await _get_interface(client, interface_id=created_interface_id)
    interface_data = get_response.json()
    assert interface_data["name"] == "overridden_name"


@pytest.mark.anyio
async def test_import_interface_template_with_valid_schema_overwrite_existing(
    client: AsyncClient,
):
    """Test importing interface template with valid schema and overwrite existing"""
    # Create existing interface
    existing_response = await _create_test_interface(
        client,
        name="existing_interface",
        color="#FF0000",
    )

    template = {
        "name": "existing_interface",
        "color": "#00FF00",  # Different color
        "tabs": [
            {
                "name": "new_tab",
                "tiles": [],
            },
        ],
        "template_version": "1.0",
    }

    # First try without overwrite
    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "overwrite_existing": False,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/interfaces/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 409
    data = response.json()
    assert "detail" in data
    assert "already exists" in data["detail"]

    # Now try with overwrite
    import_request["overwrite_existing"] = True

    response = await client.post(
        "/v0/interfaces/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
async def test_import_interface_template_with_valid_schema_complex_structure(
    client: AsyncClient,
):
    """Test importing interface template with valid schema containing complex structure"""
    template = {
        "name": "complex_interface",
        "color": "#FF0000",
        "tabs": [
            {
                "name": "dashboard_tab",
                "visible": True,
                "active": True,
                "order": 0,
                "tiles": [
                    {
                        "name": "data_table",
                        "position": {"x": 0, "y": 0, "width": 8, "height": 6},
                        "type": "Table",
                        "visible": True,
                        "table_tile": {
                            "table_type": "advanced",
                            "page_number": "1",
                            "column_order": '["col1", "col2", "col3"]',
                            "sorting": '{"col1": "asc"}',
                            "selected": "row1,row2",
                        },
                    },
                    {
                        "name": "trend_chart",
                        "position": {"x": 8, "y": 0, "width": 4, "height": 6},
                        "type": "Plot",
                        "visible": True,
                        "plot_tile": {
                            "plot_type": "line",
                            "x_axis": "timestamp",
                            "y_axis": "value",
                            "plot_scale_x": "time",
                            "plot_scale_y": "linear",
                            "plot_group_by": "category",
                        },
                    },
                ],
            },
            {
                "name": "analysis_tab",
                "visible": True,
                "active": False,
                "order": 1,
                "tiles": [
                    {
                        "name": "code_editor",
                        "position": {"x": 0, "y": 0, "width": 6, "height": 8},
                        "type": "Editor",
                        "visible": True,
                        "editor_tile": {
                            "file_type": "python",
                            "file_name": "analysis.py",
                            "content": "import pandas as pd\nprint('Analysis code')",
                        },
                    },
                    {
                        "name": "terminal",
                        "position": {"x": 6, "y": 0, "width": 6, "height": 4},
                        "type": "Terminal",
                        "visible": True,
                        "terminal_tile": {
                            "shell_type": "bash",
                        },
                    },
                    {
                        "name": "markdown_view",
                        "position": {"x": 6, "y": 4, "width": 6, "height": 4},
                        "type": "View",
                        "visible": True,
                        "view_tile": {
                            "base_index": "markdown",
                        },
                    },
                ],
            },
        ],
        "active_tab_name": "dashboard_tab",
        "template_version": "1.0",
        "description": "Complex interface with multiple tile types",
        "tags": ["complex", "dashboard", "analysis"],
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/interfaces/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["interfaces"] == 1
    assert data["import_stats"]["tabs"] == 2
    assert data["import_stats"]["tiles"] == 5

    # Verify the complex structure was imported correctly
    created_interface_id = data["created_ids"]["interface_id"]
    get_response = await _get_interface(client, interface_id=created_interface_id)
    interface_data = get_response.json()

    assert interface_data["name"] == "complex_interface"
    assert len(interface_data["tabs"]) == 2

    # Verify active tab is set correctly
    active_tab = next((tab for tab in interface_data["tabs"] if tab["active"]), None)
    assert active_tab is not None
    assert active_tab["name"] == "dashboard_tab"

    # Verify tile types and specialized data
    dashboard_tab = next(
        (tab for tab in interface_data["tabs"] if tab["name"] == "dashboard_tab"),
        None,
    )
    assert len(dashboard_tab["tiles"]) == 2

    analysis_tab = next(
        (tab for tab in interface_data["tabs"] if tab["name"] == "analysis_tab"),
        None,
    )
    assert len(analysis_tab["tiles"]) == 3

    # Verify specialized tile data was preserved
    for tile in analysis_tab["tiles"]:
        if tile["type"] == "Editor":
            assert "editor_tile" in tile
            assert tile["editor_tile"]["file_type"] == "python"
            assert "Analysis code" in tile["editor_tile"]["content"]
        elif tile["type"] == "Terminal":
            assert "terminal_tile" in tile
            assert tile["terminal_tile"]["shell_type"] == "bash"
        elif tile["type"] == "View":
            assert "view_tile" in tile
            assert tile["view_tile"]["base_index"] == "markdown"


@pytest.mark.anyio
async def test_export_import_interface_template_with_valid_schema_roundtrip(
    client: AsyncClient,
):
    """Test exporting and then importing an interface template with valid schema (roundtrip)"""
    # Create complex interface structure
    interface_response = await _create_test_interface(client, color="#FF00FF")
    interface_id = interface_response.json()["id"]

    # Create tabs and tiles
    tab_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "roundtrip_tab",
            "visible": True,
            "active": True,
            "order": 0,
            "color": "#00FF00",
        },
    )
    tab_id = tab_response.json()["id"]

    from orchestra.tests.test_tile import (
        _create_test_editor_tile,
        _create_test_table_tile,
    )

    await _create_test_table_tile(
        client,
        tab_id,
        name="roundtrip_table",
        table_type="advanced",
        page_number="2",
    )
    await _create_test_editor_tile(
        client,
        tab_id,
        name="roundtrip_editor",
        file_type="javascript",
        content="console.log('roundtrip test');",
    )

    # Export the interface template
    export_request = {
        "interface_id": interface_id,
        "include_metadata": True,
        "description": "Roundtrip test template",
        "tags": ["roundtrip", "test"],
    }

    export_response = await client.post(
        "/v0/interfaces/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert export_response.status_code == 200
    exported_template = export_response.json()["template"]

    print(exported_template)

    # Delete the original interface
    await _delete_interface(client, interface_id=interface_id)

    # Import the template back
    import_request = {
        "project": TEST_PROJECT,
        "template": exported_template,
        "validate_first": False,
        "auto_sanitize": False,
    }

    import_response = await client.post(
        "/v0/interfaces/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert import_response.status_code == 200
    import_data = import_response.json()

    assert import_data["success"] is True
    assert import_data["import_stats"]["interfaces"] == 1
    assert import_data["import_stats"]["tabs"] == 1
    assert import_data["import_stats"]["tiles"] == 2

    print(import_data)

    # Verify the imported interface matches the original
    created_interface_id = import_data["created_ids"]["interface_id"]
    get_response = await _get_interface(client, interface_id=created_interface_id)
    imported_interface = get_response.json()

    assert imported_interface["name"] == TEST_INTERFACE
    assert imported_interface["color"] == "#FF00FF"
    assert len(imported_interface["tabs"]) == 1

    tab = imported_interface["tabs"][0]
    assert tab["name"] == "roundtrip_tab"
    assert tab["color"] == "#00FF00"
    assert len(tab["tiles"]) == 2

    # Verify tile data was preserved
    tile_names = [tile["name"] for tile in tab["tiles"]]
    assert "roundtrip_table" in tile_names
    assert "roundtrip_editor" in tile_names

    editor_tile = next(
        (tile for tile in tab["tiles"] if tile["type"] == "Editor"),
        None,
    )
    assert editor_tile is not None
    assert "roundtrip test" in editor_tile["editor_tile"]["content"]


@pytest.mark.anyio
async def test_import_interface_template_with_valid_schema_empty_template(
    client: AsyncClient,
):
    """Test importing interface template with valid schema containing empty template"""
    empty_template = {
        "name": "empty_interface",
        "tabs": [],
        "template_version": "1.0",
        "description": "Empty interface template",
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": empty_template,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/interfaces/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["interfaces"] == 1
    assert data["import_stats"]["tabs"] == 0
    assert data["import_stats"]["tiles"] == 0

    # Verify empty interface was created
    created_interface_id = data["created_ids"]["interface_id"]
    get_response = await _get_interface(client, interface_id=created_interface_id)
    interface_data = get_response.json()

    assert interface_data["name"] == "empty_interface"
    assert len(interface_data["tabs"]) == 0


@pytest.mark.anyio
async def test_export_interface_template_with_valid_schema_multiple_active_tabs(
    client: AsyncClient,
):
    """Test exporting interface template with valid schema handling multiple tabs with active states"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create multiple tabs, set one as active
    tab1_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "tab_1",
            "active": True,
            "order": 0,
        },
    )
    tab1_id = tab1_response.json()["id"]

    tab2_response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": "tab_2",
            "active": False,
            "order": 1,
        },
    )

    # Update interface to set active tab
    await _update_interface(
        client,
        interface_id=interface_id,
        update_data={"active_tab_id": tab1_id},
    )

    export_request = {
        "interface_id": interface_id,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/interfaces/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    template = response.json()["template"]

    # Verify active tab is correctly identified by name
    assert template["active_tab_name"] == "tab_1"

    # Verify tab active states
    tab1_template = next(
        (tab for tab in template["tabs"] if tab["name"] == "tab_1"),
        None,
    )
    tab2_template = next(
        (tab for tab in template["tabs"] if tab["name"] == "tab_2"),
        None,
    )

    assert tab1_template["active"] is True
    assert tab2_template["active"] is False


@pytest.mark.anyio
async def test_interface_context_validation_valid_reference(client: AsyncClient):
    """Test that interface API validates context field with valid reference"""
    project_name = f"test-interface-context-{uuid.uuid4()}"
    context_name = "valid-context"

    # Create project and context
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Valid context"},
        headers=HEADERS,
    )

    # Create interface with valid context
    response = await client.post(
        "/v0/interfaces/",
        json={
            "name": "test-interface",
            "project": project_name,
            "context": context_name,
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    interface_id = response.json()["id"]

    # Update with valid context field - should work
    response = await client.put(
        "/v0/interfaces/",
        params={"interface_id": interface_id},
        json={
            "context": context_name,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Clean up
    await _delete_project(client, project_name)


@pytest.mark.anyio
async def test_interface_context_validation_invalid_reference(client: AsyncClient):
    """Test that interface API rejects invalid context references"""
    project_name = f"test-interface-invalid-context-{uuid.uuid4()}"
    invalid_context = "nonexistent-context"

    # Create project only (no context)
    await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=HEADERS,
    )

    # Try to create interface with invalid context
    response = await client.post(
        "/v0/interfaces/",
        json={
            "name": "test-interface",
            "project": project_name,
            "context": invalid_context,
        },
        headers=HEADERS,
    )
    # Should fail with validation error
    assert response.status_code == 400
    assert f"Context '{invalid_context}' not found" in response.json()["detail"]

    # Clean up
    await _delete_project(client, project_name)
