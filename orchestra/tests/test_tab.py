import os
import uuid

import pytest
from httpx import AsyncClient

from orchestra.tests.test_tile import _create_test_tile, _get_tile

# Common headers and data
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

# Test project and resource names
TEST_PROJECT = "test-tab-project"
TEST_INTERFACE = "test-interface"
TEST_TAB = "test-tab"
TEST_DESCRIPTION = "Test description"


# Helper functions for project and interface creation
async def _create_project(client: AsyncClient, project_name=TEST_PROJECT):
    """Create a test project"""
    response = await client.post(
        "/v0/project",
        json={"name": project_name, "description": TEST_DESCRIPTION},
        headers=HEADERS,
    )
    assert response.status_code in [
        200,
        201,
    ], f"Failed to create project: {response.json()}"
    return response


async def _delete_project(client: AsyncClient, project_name=TEST_PROJECT):
    """Delete a test project"""
    response = await client.delete(f"/v0/project/{project_name}", headers=HEADERS)
    return response


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
    assert response.status_code == 201, f"Failed to create interface: {response.json()}"
    return response


# Tab helpers
async def _create_test_tab(
    client: AsyncClient,
    interface_id,
    name=TEST_TAB,
    active=True,
    order=0,
    color="#00FF00",
    tab_id=None,
):
    """Create a test tab"""
    payload = {
        "interface_id": interface_id,
        "name": name,
        "active": active,
        "order": order,
        "visible": True,
        "color": color,
    }

    # Add the tab_id if provided
    if tab_id:
        payload["tab_id"] = tab_id

    response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json=payload,
    )
    return response


async def _get_tab(client: AsyncClient, tab_id=None, interface_id=None, name=None):
    """
    Get tab by ID or by interface_id and name

    If tab_id is provided, gets a single tab by ID
    If interface_id and name are provided, gets a single tab by interface_id and name
    """
    if tab_id:
        return await client.get(f"/v0/tab/?tab_id={tab_id}", headers=HEADERS)
    elif interface_id and name:
        return await client.get(
            f"/v0/tab/?interface_id={interface_id}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either tab_id or interface_id+name")


async def _list_tabs(client: AsyncClient, interface_id=None, name=None):
    """List tabs for an interface"""
    params = {}
    if interface_id:
        params["interface_id"] = interface_id
    else:
        raise ValueError("Must provide interface_id")

    # Construct the URL with parameters
    param_str = "&".join([f"{k}={v}" for k, v in params.items()])
    return await client.get(f"/v0/tab/list?{param_str}", headers=HEADERS)


async def _update_tab(
    client: AsyncClient,
    tab_id=None,
    interface_id=None,
    name=None,
    update_data=None,
):
    """Update tab by ID or by interface_id and name"""
    if update_data is None:
        update_data = {}

    if tab_id:
        return await client.put(
            f"/v0/tab/?tab_id={tab_id}",
            headers=HEADERS,
            json=update_data,
        )
    elif interface_id and name:
        return await client.put(
            f"/v0/tab/?interface_id={interface_id}&name={name}",
            headers=HEADERS,
            json=update_data,
        )
    else:
        raise ValueError("Must provide either tab_id or interface_id+name")


async def _delete_tab(client: AsyncClient, tab_id=None, interface_id=None, name=None):
    """Delete tab by ID or by interface_id and name"""
    if tab_id:
        return await client.delete(f"/v0/tab/?tab_id={tab_id}", headers=HEADERS)
    elif interface_id and name:
        return await client.delete(
            f"/v0/tab/?interface_id={interface_id}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either tab_id or interface_id+name")


async def _create_tab_checkpoint(
    client: AsyncClient,
    tab_id=None,
    interface_id=None,
    name=None,
):
    """Create a checkpoint for a tab"""
    if tab_id:
        return await client.post(f"/v0/tab/checkpoint?tab_id={tab_id}", headers=HEADERS)
    elif interface_id and name:
        return await client.post(
            f"/v0/tab/checkpoint?interface_id={interface_id}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either tab_id or interface_id+name")


async def _get_tab_checkpoint(
    client: AsyncClient,
    tab_id=None,
    interface_id=None,
    name=None,
):
    """Get the latest checkpoint for a tab"""
    if tab_id:
        return await client.get(f"/v0/tab/checkpoint?tab_id={tab_id}", headers=HEADERS)
    elif interface_id and name:
        return await client.get(
            f"/v0/tab/checkpoint?interface_id={interface_id}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either tab_id or interface_id+name")


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


# Tab Tests
@pytest.mark.anyio
async def test_create_tab(client: AsyncClient):
    """Test creating a tab"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create a tab
    response = await _create_test_tab(client, interface_id)
    assert response.status_code == 201

    data = response.json()
    assert data["name"] == TEST_TAB
    assert data["interface_id"] == interface_id
    assert data["active"] is True
    assert data["visible"] is True
    assert data["order"] == 0
    assert data["color"] == "#00FF00"
    assert data["is_checkpoint"] is False
    assert "id" in data
    assert "created_at" in data


@pytest.mark.anyio
async def test_get_tab_by_id(client: AsyncClient):
    """Test getting a tab by ID"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Get the tab by ID
    response = await _get_tab(client, tab_id=tab_id)
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == tab_id
    assert data["name"] == TEST_TAB
    assert data["interface_id"] == interface_id


@pytest.mark.anyio
async def test_get_tab_by_interface_and_name(client: AsyncClient):
    """Test getting a tab by interface_id and name"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)

    # Get the tab by interface_id and name
    response = await _get_tab(client, interface_id=interface_id, name=TEST_TAB)
    assert response.status_code == 200

    data = response.json()
    assert data["name"] == TEST_TAB
    assert data["interface_id"] == interface_id


@pytest.mark.anyio
async def test_list_tabs(client: AsyncClient):
    """Test listing tabs for an interface"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create multiple tabs
    await _create_test_tab(
        client,
        interface_id,
        name="list-tab-1",
        active=True,
        order=0,
    )
    await _create_test_tab(
        client,
        interface_id,
        name="list-tab-2",
        active=False,
        order=1,
    )

    # List tabs
    response = await _list_tabs(client, interface_id=interface_id)
    assert response.status_code == 200

    data = response.json()
    assert len(data) == 2
    tab_names = [tab["name"] for tab in data]
    assert "list-tab-1" in tab_names
    assert "list-tab-2" in tab_names


@pytest.mark.anyio
async def test_update_tab_by_id(client: AsyncClient):
    """Test updating a tab by ID"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Update the tab by ID
    new_name = "updated-tab"
    update_data = {"name": new_name, "visible": False, "color": "#0000FF"}
    response = await _update_tab(client, tab_id=tab_id, update_data=update_data)
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == tab_id
    assert data["name"] == new_name
    assert data["visible"] is False
    assert data["color"] == "#0000FF"


@pytest.mark.anyio
async def test_update_tab_by_interface_and_name(client: AsyncClient):
    """Test updating a tab by interface_id and name"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)

    # Update the tab by interface_id and name
    update_data = {"visible": False, "color": "#0000FF"}
    response = await _update_tab(
        client,
        interface_id=interface_id,
        name=TEST_TAB,
        update_data=update_data,
    )
    assert response.status_code == 200

    data = response.json()
    assert data["name"] == TEST_TAB
    assert data["visible"] is False
    assert data["color"] == "#0000FF"


@pytest.mark.anyio
async def test_delete_tab_by_id(client: AsyncClient):
    """Test deleting a tab by ID"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Delete the tab by ID
    response = await _delete_tab(client, tab_id=tab_id)
    assert response.status_code == 200

    # Verify tab is deleted
    get_response = await _get_tab(client, tab_id=tab_id)
    assert get_response.status_code == 404


@pytest.mark.anyio
async def test_delete_tab_by_interface_and_name(client: AsyncClient):
    """Test deleting a tab by interface_id and name"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)

    # Delete the tab by interface_id and name
    response = await _delete_tab(client, interface_id=interface_id, name=TEST_TAB)
    assert response.status_code == 200

    # Verify tab is deleted
    get_response = await _get_tab(client, interface_id=interface_id, name=TEST_TAB)
    assert get_response.status_code == 404


@pytest.mark.anyio
async def test_tab_checkpoint_by_id(client: AsyncClient):
    """Test creating tab checkpoints by ID"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a checkpoint by ID
    response = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert response.status_code == 200

    checkpoint_data = response.json()
    assert checkpoint_data["is_checkpoint"] is True
    assert checkpoint_data["name"] == TEST_TAB


@pytest.mark.anyio
async def test_tab_checkpoint_by_interface_and_name(client: AsyncClient):
    """Test creating tab checkpoints by interface_id and name"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)

    # Create a checkpoint by interface_id and name
    response = await _create_tab_checkpoint(
        client,
        interface_id=interface_id,
        name=TEST_TAB,
    )
    assert response.status_code == 200

    checkpoint_data = response.json()
    assert checkpoint_data["is_checkpoint"] is True
    assert checkpoint_data["name"] == TEST_TAB


@pytest.mark.anyio
async def test_get_tab_checkpoint(client: AsyncClient):
    """Test retrieving tab checkpoints"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a checkpoint
    await _create_tab_checkpoint(client, tab_id=tab_id)

    # Get the checkpoint
    response = await _get_tab_checkpoint(client, tab_id=tab_id)
    assert response.status_code == 200

    data = response.json()
    assert data["is_checkpoint"] is True
    assert data["name"] == TEST_TAB


@pytest.mark.anyio
async def test_create_duplicate_tab(client: AsyncClient):
    """Test creating a duplicate tab (should fail)"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)

    # Try to create another tab with the same name
    response = await _create_test_tab(client, interface_id)
    assert response.status_code == 409


@pytest.mark.anyio
async def test_tab_with_nonexistent_interface(client: AsyncClient):
    """Test creating a tab with a non-existent interface (should fail)"""
    non_existent_interface = str(uuid.uuid4())  # random ID
    response = await _create_test_tab(client, non_existent_interface)
    assert response.status_code == 404


@pytest.mark.anyio
async def test_tab_ordering(client: AsyncClient):
    """Test tab ordering when multiple tabs are created"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create tabs with different order values
    await _create_test_tab(client, interface_id, name="order-tab-1", order=1)
    await _create_test_tab(client, interface_id, name="order-tab-0", order=0)
    await _create_test_tab(client, interface_id, name="order-tab-2", order=2)

    # List tabs and verify order
    response = await _list_tabs(client, interface_id=interface_id)
    assert response.status_code == 200

    data = response.json()
    assert len(data) == 3

    # Check that tabs are returned in order based on the order field
    assert data[0]["name"] == "order-tab-0"
    assert data[0]["order"] == 0

    assert data[1]["name"] == "order-tab-1"
    assert data[1]["order"] == 1

    assert data[2]["name"] == "order-tab-2"
    assert data[2]["order"] == 2


@pytest.mark.anyio
async def test_tab_active_flag(client: AsyncClient):
    """Test setting a tab as active deactivates other tabs"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create two tabs, one active and one inactive
    tab1_response = await _create_test_tab(
        client,
        interface_id,
        name="active-tab",
        active=True,
    )
    tab2_response = await _create_test_tab(
        client,
        interface_id,
        name="inactive-tab",
        active=False,
    )

    # Get tab IDs
    tab1_id = tab1_response.json()["id"]
    tab2_id = tab2_response.json()["id"]

    # Verify tab1 is active and tab2 is inactive
    response1 = await _get_tab(client, tab_id=tab1_id)
    response2 = await _get_tab(client, tab_id=tab2_id)

    assert response1.json()["active"] is True
    assert response2.json()["active"] is False

    # Update tab2 to be active
    update_data = {"active": True}
    response = await _update_tab(client, tab_id=tab2_id, update_data=update_data)
    assert response.status_code == 200
    assert response.json()["active"] is True

    # Verify tab1 is now inactive
    response1 = await _get_tab(client, tab_id=tab1_id)
    assert response1.json()["active"] is False


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_existing_checkpoint(client: AsyncClient):
    """Test creating a checkpoint when one already exists (should update existing checkpoint)"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert first_checkpoint.status_code == 200
    first_checkpoint_id = first_checkpoint.json()["id"]

    # Update the original tab
    update_response = await _update_tab(
        client,
        tab_id=tab_id,
        update_data={"color": "#00FF00"},
    )
    assert update_response.status_code == 200

    # Create second checkpoint (should update existing one)
    second_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert second_checkpoint.status_code == 200
    second_checkpoint_id = second_checkpoint.json()["id"]

    # Verify it's the same checkpoint (same ID)
    assert first_checkpoint_id == second_checkpoint_id
    # Verify it has the updated color
    assert second_checkpoint.json()["color"] == "#00FF00"


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_tiles(client: AsyncClient):
    """Test creating a checkpoint for a tab with tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a tile in the tab
    from orchestra.tests.test_tile import _create_test_tile

    tile_response = await _create_test_tile(client, tab_id, name="test-tile")
    assert tile_response.status_code == 201

    # Create a checkpoint
    checkpoint_response = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert checkpoint_response.status_code == 200

    # Verify the checkpoint has the tile
    checkpoint_data = checkpoint_response.json()
    assert len(checkpoint_data["tiles"]) == 1
    assert checkpoint_data["tiles"][0]["name"] == "test-tile"
    assert checkpoint_data["tiles"][0]["visible"] is True


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_multiple_tiles(client: AsyncClient):
    """Test creating a checkpoint for a tab with multiple tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create multiple tiles
    tile_names = ["tile1", "tile2", "tile3"]
    from orchestra.tests.test_tile import _create_test_tile

    for i, name in enumerate(tile_names):
        tile_response = await _create_test_tile(client, tab_id, name=name)
        assert tile_response.status_code == 201

    # Create a checkpoint
    checkpoint_response = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert checkpoint_response.status_code == 200

    # Verify all tiles are in the checkpoint
    checkpoint_data = checkpoint_response.json()
    assert len(checkpoint_data["tiles"]) == len(tile_names)
    checkpoint_tile_names = [tile["name"] for tile in checkpoint_data["tiles"]]
    assert set(checkpoint_tile_names) == set(tile_names)


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_invisible_tiles(client: AsyncClient):
    """Test creating a checkpoint for a tab with invisible tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a tile with visible=False
    from orchestra.tests.test_tile import _create_test_tile

    tile_response = await _create_test_tile(
        client,
        tab_id,
        name="invisible-tile",
        visible=False,
    )
    assert tile_response.status_code == 201

    # Create a checkpoint
    checkpoint_response = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert checkpoint_response.status_code == 200

    # Verify the checkpoint preserves the tile's visibility
    checkpoint_data = checkpoint_response.json()
    assert len(checkpoint_data["tiles"]) == 1
    assert checkpoint_data["tiles"][0]["visible"] is False


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_tile_updates(client: AsyncClient):
    """Test creating a checkpoint after updating tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a tile
    from orchestra.tests.test_tile import _create_test_tile

    tile_response = await _create_test_tile(client, tab_id, name="test-tile")
    assert tile_response.status_code == 201
    tile_id = tile_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert first_checkpoint.status_code == 200

    # Update the tile
    update_tile_response = await client.put(
        f"/v0/tile/?tile_id={tile_id}",
        headers=HEADERS,
        json={
            "visible": False,
        },
    )
    assert update_tile_response.status_code == 200

    # Create second checkpoint
    second_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has the updated tile properties
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tiles"]) == 1
    assert checkpoint_data["tiles"][0]["visible"] is False


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_deleted_tiles(client: AsyncClient):
    """Test creating a checkpoint after deleting tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a tile
    from orchestra.tests.test_tile import _create_test_tile

    tile_response = await _create_test_tile(client, tab_id, name="test-tile")
    assert tile_response.status_code == 201
    tile_id = tile_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert first_checkpoint.status_code == 200

    # Delete the tile
    delete_tile_response = await client.delete(
        f"/v0/tile/?tile_id={tile_id}",
        headers=HEADERS,
    )
    assert delete_tile_response.status_code == 200

    # Create second checkpoint
    second_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has no tiles
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tiles"]) == 0


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_added_tiles(client: AsyncClient):
    """Test creating a checkpoint after adding new tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create first checkpoint (no tiles)
    first_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert first_checkpoint.status_code == 200
    assert len(first_checkpoint.json()["tiles"]) == 0

    # Add a tile
    from orchestra.tests.test_tile import _create_test_tile

    tile_response = await _create_test_tile(client, tab_id, name="new-tile")
    assert tile_response.status_code == 201

    # Create second checkpoint
    second_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has the new tile
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tiles"]) == 1
    assert checkpoint_data["tiles"][0]["name"] == "new-tile"


@pytest.mark.anyio
async def test_create_tab_checkpoint_with_renamed_tiles(client: AsyncClient):
    """Test creating a checkpoint after renaming tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a tile
    from orchestra.tests.test_tile import _create_test_tile

    tile_response = await _create_test_tile(client, tab_id)
    assert tile_response.status_code == 201
    tile_id = tile_response.json()["id"]

    # Create first checkpoint
    first_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert first_checkpoint.status_code == 200

    # Rename the tile
    update_tile_response = await client.put(
        f"/v0/tile/?tile_id={tile_id}",
        headers=HEADERS,
        json={"name": "new-name"},
    )
    assert update_tile_response.status_code == 200

    # Create second checkpoint
    second_checkpoint = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert second_checkpoint.status_code == 200

    # Verify the second checkpoint has the renamed tile
    checkpoint_data = second_checkpoint.json()
    assert len(checkpoint_data["tiles"]) == 1
    assert checkpoint_data["tiles"][0]["name"] == "new-name"


@pytest.mark.anyio
async def test_create_tab_with_specified_id(client: AsyncClient):
    """Test creating a tab with a user-specified ID"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Generate a UUID to use for the tab
    specified_id = str(uuid.uuid4())

    # Create a tab with the specified ID
    response = await _create_test_tab(
        client,
        interface_id,
        name="predetermined-id-tab",
        tab_id=specified_id,
    )
    assert response.status_code == 201

    data = response.json()
    assert data["id"] == specified_id
    assert data["name"] == "predetermined-id-tab"
    assert data["interface_id"] == interface_id

    # Verify we can retrieve the tab by its ID
    get_response = await _get_tab(client, tab_id=specified_id)
    assert get_response.status_code == 200
    get_data = get_response.json()
    assert get_data["id"] == specified_id


@pytest.mark.anyio
async def test_restore_tab_checkpoint(client: AsyncClient):
    """Ensure tab checkpoint snapshot is not affected by later updates to the active tab."""
    # 1. Create interface and original tab
    interface_resp = await _create_test_interface(client)
    interface_id = interface_resp.json()["id"]

    create_tab_resp = await _create_test_tab(
        client,
        interface_id,
        name="checkpoint-tab",
        color="#00FF00",
        active=True,
        order=1,
    )
    assert create_tab_resp.status_code == 201
    tab_id = create_tab_resp.json()["id"]

    # 2. Create a checkpoint for this tab
    checkpoint_resp = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert checkpoint_resp.status_code == 200
    checkpoint_id = checkpoint_resp.json()["id"]

    # Verify initial properties in checkpoint
    assert checkpoint_resp.json()["color"] == "#00FF00"
    assert checkpoint_resp.json()["visible"] is True
    assert checkpoint_resp.json()["is_checkpoint"] is True

    # 3. Update the active tab's properties
    update_payload = {"color": "#FF00FF", "visible": False, "order": 5}
    upd_resp = await _update_tab(client, tab_id=tab_id, update_data=update_payload)
    assert upd_resp.status_code == 200
    assert upd_resp.json()["color"] == "#FF00FF"
    assert upd_resp.json()["visible"] is False
    assert upd_resp.json()["order"] == 5

    # 4. Fetch checkpoint again and verify values unchanged
    cp_get = await _get_tab_checkpoint(client, tab_id=tab_id)
    assert cp_get.status_code == 200
    cp_data = cp_get.json()

    assert cp_data["color"] == "#00FF00"  # original color
    assert cp_data["visible"] is True  # original visibility
    assert cp_data["order"] == 1  # original order
    assert cp_data["is_checkpoint"] is True

    # Active tab should reflect new values
    active_get = await _get_tab(client, tab_id=tab_id)
    assert active_get.status_code == 200
    active_data = active_get.json()
    assert active_data["color"] == "#FF00FF"
    assert active_data["visible"] is False
    assert active_data["order"] == 5


@pytest.mark.anyio
async def test_delete_tab_by_id_with_tiles(client: AsyncClient):
    """Test deleting a tab by ID and ensuring associated tiles are also deleted"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create multiple tiles in the tab
    tile_ids = []
    for i in range(3):
        tile_response = await _create_test_tile(
            client,
            tab_id,
            name=f"test_tile_{i}",
            tile_type="Table",
            table_tile_data={
                "table_type": "basic",
                "page_number": "1",
            },
        )
        assert tile_response.status_code == 201
        tile_ids.append(tile_response.json()["id"])

    # Verify tiles exist before deletion
    for tile_id in tile_ids:
        get_tile_response = await _get_tile(client, tile_id=tile_id)
        assert get_tile_response.status_code == 200

    # Delete the tab by ID
    response = await _delete_tab(client, tab_id=tab_id)
    assert response.status_code == 200

    # Verify tab is deleted
    get_tab_response = await _get_tab(client, tab_id=tab_id)
    assert get_tab_response.status_code == 404

    # Verify all tiles are also deleted
    for tile_id in tile_ids:
        get_tile_response = await _get_tile(client, tile_id=tile_id)
        assert get_tile_response.status_code == 404


@pytest.mark.anyio
async def test_delete_tab_by_interface_and_name_with_tiles(client: AsyncClient):
    """Test deleting a tab by interface_id and name and ensuring associated tiles are also deleted"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)

    # Get the tab ID for tile creation
    get_tab_response = await _get_tab(client, interface_id=interface_id, name=TEST_TAB)
    assert get_tab_response.status_code == 200
    tab_id = get_tab_response.json()["id"]

    # Create multiple tiles in the tab with different types
    tile_ids = []

    # Create a Table tile
    table_tile_response = await _create_test_tile(
        client,
        tab_id,
        name="table_tile",
        tile_type="Table",
        table_tile_data={
            "table_type": "advanced",
            "page_number": "2",
            "selected": "Entries/id_123,Entries/id_456",
        },
    )
    assert table_tile_response.status_code == 201
    tile_ids.append(table_tile_response.json()["id"])

    # Create a Plot tile
    plot_tile_response = await _create_test_tile(
        client,
        tab_id,
        name="plot_tile",
        tile_type="Plot",
        plot_tile_data={
            "plot_type": "scatter",
            "x_axis": "time",
            "y_axis": "value",
            "plot_scale_x": "linear",
        },
    )
    assert plot_tile_response.status_code == 201
    tile_ids.append(plot_tile_response.json()["id"])

    # Create an Editor tile
    editor_tile_response = await _create_test_tile(
        client,
        tab_id,
        name="editor_tile",
        tile_type="Editor",
        editor_tile_data={
            "file_type": "python",
            "file_name": "test.py",
            "content": "print('Hello World')",
        },
    )
    assert editor_tile_response.status_code == 201
    tile_ids.append(editor_tile_response.json()["id"])

    # Verify tiles exist before deletion
    for tile_id in tile_ids:
        get_tile_response = await _get_tile(client, tile_id=tile_id)
        assert get_tile_response.status_code == 200

    # Delete the tab by interface_id and name
    response = await _delete_tab(client, interface_id=interface_id, name=TEST_TAB)
    assert response.status_code == 200

    # Verify tab is deleted
    get_tab_response = await _get_tab(client, interface_id=interface_id, name=TEST_TAB)
    assert get_tab_response.status_code == 404

    # Verify all tiles are also deleted
    for tile_id in tile_ids:
        get_tile_response = await _get_tile(client, tile_id=tile_id)
        assert get_tile_response.status_code == 404


@pytest.mark.anyio
async def test_export_tab_template_with_valid_schema(client: AsyncClient):
    """Test exporting a tab template with valid schema"""
    # Create interface and tab with tiles
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(
        client,
        interface_id,
        name="export_tab",
        color="#FF0000",
        order=1,
    )
    tab_id = tab_response.json()["id"]

    # Create various types of tiles
    from orchestra.tests.test_tile import (
        _create_test_editor_tile,
        _create_test_plot_tile,
        _create_test_table_tile,
        _create_test_view_tile,
    )

    await _create_test_table_tile(
        client,
        tab_id,
        name="data_table",
        table_type="advanced",
        page_number="2",
    )
    await _create_test_plot_tile(
        client,
        tab_id,
        name="trend_plot",
        plot_type="line",
        x_axis="time",
        y_axis="value",
    )
    await _create_test_view_tile(
        client,
        tab_id,
        name="markdown_view",
        base_index="markdown",
    )
    await _create_test_editor_tile(
        client,
        tab_id,
        name="code_editor",
        file_type="python",
        content="print('test')",
    )

    # Export tab template
    export_request = {
        "tab_id": tab_id,
        "include_metadata": True,
        "description": "Test tab template",
        "tags": ["test", "tab"],
        "template_name": "Test Tab Template",
    }

    response = await client.post(
        "/v0/tab/export_template",
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
    assert template["name"] == "export_tab"
    assert template["color"] == "#FF0000"
    assert template["order"] == 1
    assert len(template["tiles"]) == 4

    # Verify tiles are exported with correct types
    tile_types = [tile["type"] for tile in template["tiles"]]
    assert "Table" in tile_types
    assert "Plot" in tile_types
    assert "View" in tile_types
    assert "Editor" in tile_types

    # Verify specialized tile data is preserved
    table_tile = next(
        (tile for tile in template["tiles"] if tile["type"] == "Table"),
        None,
    )
    assert table_tile["table_tile"]["table_type"] == "advanced"
    assert table_tile["table_tile"]["page_number"] == "2"

    plot_tile = next(
        (tile for tile in template["tiles"] if tile["type"] == "Plot"),
        None,
    )
    assert plot_tile["plot_tile"]["plot_type"] == "line"
    assert plot_tile["plot_tile"]["x_axis"] == "time"

    # Verify export stats
    stats = data["export_stats"]
    assert stats["tiles"] == 4


@pytest.mark.anyio
async def test_export_tab_template_with_valid_schema_by_interface_and_name(
    client: AsyncClient,
):
    """Test exporting tab template with valid schema using interface_id and name"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    await _create_test_tab(client, interface_id, name="named_tab")

    export_request = {
        "interface_id": interface_id,
        "tab_name": "named_tab",
        "include_metadata": True,
        "description": "Export by interface and name",
    }

    response = await client.post(
        "/v0/tab/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    template = data["template"]
    assert template["name"] == "named_tab"
    assert template["description"] == "Export by interface and name"


@pytest.mark.anyio
async def test_export_tab_template_with_valid_schema_checkpoint(client: AsyncClient):
    """Test exporting tab template with valid schema from checkpoint"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(
        client,
        interface_id,
        name="checkpoint_tab",
        color="#FF0000",
    )
    tab_id = tab_response.json()["id"]

    # Create checkpoint
    checkpoint_response = await _create_tab_checkpoint(client, tab_id=tab_id)
    assert checkpoint_response.status_code == 200

    # Update original tab after checkpoint
    await _update_tab(client, tab_id=tab_id, update_data={"color": "#00FF00"})

    # Export from checkpoint
    checkpoint_tab_id = checkpoint_response.json()["id"]
    export_request = {
        "tab_id": checkpoint_tab_id,
        "checkpoint": True,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/tab/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    template = response.json()["template"]

    # Should export checkpoint version (original color)
    assert template["color"] == "#FF0000"


@pytest.mark.anyio
async def test_export_tab_template_with_valid_schema_empty_tab(client: AsyncClient):
    """Test exporting tab template with valid schema from empty tab"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id, name="empty_tab")
    tab_id = tab_response.json()["id"]

    export_request = {
        "tab_id": tab_id,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/tab/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    template = data["template"]
    assert template["name"] == "empty_tab"
    assert len(template["tiles"]) == 0

    stats = data["export_stats"]
    assert stats["tiles"] == 0


@pytest.mark.anyio
async def test_import_tab_template_with_valid_schema(client: AsyncClient):
    """Test importing a tab template with valid schema"""
    # Create target interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create a valid template
    template = {
        "name": "imported_tab",
        "visible": True,
        "active": True,
        "order": 2,
        "color": "#FF00FF",
        "tiles": [
            {
                "name": "imported_table",
                "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                "type": "Table",
                "visible": True,
                "table_tile": {
                    "table_type": "advanced",
                    "page_number": "1",
                    "column_order": '["col1", "col2"]',
                    "sorting": '{"col1": "desc"}',
                },
            },
            {
                "name": "imported_plot",
                "position": {"x": 6, "y": 0, "width": 6, "height": 4},
                "type": "Plot",
                "visible": True,
                "plot_tile": {
                    "plot_type": "scatter",
                    "x_axis": "x_data",
                    "y_axis": "y_data",
                    "plot_scale_x": "linear",
                    "plot_scale_y": "log",
                },
            },
            {
                "name": "imported_editor",
                "position": {"x": 0, "y": 4, "width": 12, "height": 4},
                "type": "Editor",
                "visible": True,
                "editor_tile": {
                    "file_type": "javascript",
                    "file_name": "script.js",
                    "content": "console.log('imported tab');",
                },
            },
        ],
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "interface_id": interface_id,
        "validate_first": False,  # Skip validation for v0
        "auto_sanitize": False,
        "overwrite_existing": False,
    }

    response = await client.post(
        "/v0/tab/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["tabs"] == 1
    assert data["import_stats"]["tiles"] == 3

    # Verify the tab was created
    created_tab_id = data["created_ids"]["tab_id"]
    get_response = await _get_tab(client, tab_id=created_tab_id)
    assert get_response.status_code == 200

    tab_data = get_response.json()
    assert tab_data["name"] == "imported_tab"
    assert tab_data["color"] == "#FF00FF"
    assert tab_data["order"] == 2
    assert tab_data["active"] is True
    assert len(tab_data["tiles"]) == 3

    # Verify tiles were created correctly
    tile_names = [tile["name"] for tile in tab_data["tiles"]]
    assert "imported_table" in tile_names
    assert "imported_plot" in tile_names
    assert "imported_editor" in tile_names

    # Verify specialized tile data was preserved
    editor_tile = next(
        (tile for tile in tab_data["tiles"] if tile["type"] == "Editor"),
        None,
    )
    assert editor_tile is not None
    assert editor_tile["editor_tile"]["file_type"] == "javascript"
    assert "imported tab" in editor_tile["editor_tile"]["content"]


@pytest.mark.anyio
async def test_import_tab_template_with_valid_schema_new_name(client: AsyncClient):
    """Test importing tab template with valid schema and new name override"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    template = {
        "name": "original_tab_name",
        "tiles": [
            {
                "name": "test_tile",
                "position": {"x": 0, "y": 0, "width": 4, "height": 3},
                "type": "Table",
                "table_tile": {"table_type": "basic"},
            },
        ],
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "interface_id": interface_id,
        "new_tab_name": "overridden_tab_name",
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tab/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    # Verify tab was created with new name
    created_tab_id = data["created_ids"]["tab_id"]
    get_response = await _get_tab(client, tab_id=created_tab_id)
    tab_data = get_response.json()
    assert tab_data["name"] == "overridden_tab_name"


@pytest.mark.anyio
async def test_import_tab_template_with_valid_schema_by_interface_name(
    client: AsyncClient,
):
    """Test importing tab template with valid schema using interface name"""
    await _create_test_interface(client, name="target_interface")

    template = {
        "name": "imported_by_interface_name",
        "tiles": [],
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "interface_name": "target_interface",
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tab/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["tabs"] == 1


@pytest.mark.anyio
async def test_import_tab_template_with_valid_schema_overwrite_existing(
    client: AsyncClient,
):
    """Test importing tab template with valid schema and overwrite existing"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    # Create existing tab
    existing_tab_response = await _create_test_tab(
        client,
        interface_id,
        name="existing_tab",
        color="#FF0000",
    )

    template = {
        "name": "existing_tab",
        "color": "#00FF00",  # Different color
        "tiles": [
            {
                "name": "new_tile",
                "position": {"x": 0, "y": 0, "width": 4, "height": 3},
                "type": "Table",
                "table_tile": {"table_type": "basic"},
            },
        ],
    }

    # First try without overwrite
    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "interface_id": interface_id,
        "overwrite_existing": False,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tab/import_template",
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
        "/v0/tab/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
async def test_import_tab_template_with_valid_schema_complex_tiles(client: AsyncClient):
    """Test importing tab template with valid schema containing complex tile configurations"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    template = {
        "name": "complex_tab",
        "visible": True,
        "active": True,
        "order": 0,
        "color": "#FF0000",
        "tiles": [
            {
                "name": "advanced_table",
                "position": {"x": 0, "y": 0, "width": 8, "height": 6},
                "type": "Table",
                "visible": True,
                "locked": False,
                "table_tile": {
                    "table_type": "advanced",
                    "page_number": "3",
                    "column_order": '["id", "name", "value", "timestamp"]',
                    "hidden_columns": '["internal_id"]',
                    "default_hidden_columns": False,
                    "sorting": '{"timestamp": "desc", "value": "asc"}',
                    "group_sorting": '{"category": "asc"}',
                    "columns_pin_left": '["id", "name"]',
                    "columns_pin_right": '["timestamp"]',
                    "selected": "row_1,row_5,row_10",
                },
            },
            {
                "name": "multi_series_plot",
                "position": {"x": 8, "y": 0, "width": 4, "height": 6},
                "type": "Plot",
                "visible": True,
                "plot_tile": {
                    "plot_type": "line",
                    "plot_scale_x": "time",
                    "plot_scale_y": "log",
                    "plot_aggregate": "mean",
                    "x_axis": "timestamp",
                    "y_axis": "value",
                    "plot_group_by": "category",
                    "plot_group_by_colors": '{"A": "#FF0000", "B": "#00FF00"}',
                    "bin_count": "50",
                    "regression_line": "true",
                },
            },
            {
                "name": "python_editor",
                "position": {"x": 0, "y": 6, "width": 6, "height": 6},
                "type": "Editor",
                "visible": True,
                "editor_tile": {
                    "file_type": "python",
                    "file_name": "analysis.py",
                    "content": "import pandas as pd\nimport numpy as np\n\n# Complex analysis code\ndf = pd.read_csv('data.csv')\nresult = df.groupby('category').agg({'value': ['mean', 'std']})\nprint(result)",
                },
            },
            {
                "name": "bash_terminal",
                "position": {"x": 6, "y": 6, "width": 6, "height": 3},
                "type": "Terminal",
                "visible": True,
                "terminal_tile": {
                    "shell_type": "bash",
                },
            },
            {
                "name": "html_view",
                "position": {"x": 6, "y": 9, "width": 6, "height": 3},
                "type": "View",
                "visible": True,
                "view_tile": {
                    "base_index": "html",
                },
            },
        ],
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": template,
        "interface_id": interface_id,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tab/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["tabs"] == 1
    assert data["import_stats"]["tiles"] == 5

    # Verify complex tile configurations were preserved
    created_tab_id = data["created_ids"]["tab_id"]
    get_response = await _get_tab(client, tab_id=created_tab_id)
    tab_data = get_response.json()

    assert len(tab_data["tiles"]) == 5

    # Verify advanced table configuration
    table_tile = next(
        (tile for tile in tab_data["tiles"] if tile["name"] == "advanced_table"),
        None,
    )
    assert table_tile is not None
    table_config = table_tile["table_tile"]
    assert table_config["table_type"] == "advanced"
    assert table_config["page_number"] == "3"
    assert "id" in table_config["column_order"]
    assert "internal_id" in table_config["hidden_columns"]
    assert table_config["default_hidden_columns"] is False

    # Verify plot configuration
    plot_tile = next(
        (tile for tile in tab_data["tiles"] if tile["name"] == "multi_series_plot"),
        None,
    )
    assert plot_tile is not None
    plot_config = plot_tile["plot_tile"]
    assert plot_config["plot_type"] == "line"
    assert plot_config["plot_scale_y"] == "log"
    assert plot_config["regression_line"] == "true"

    # Verify editor content
    editor_tile = next(
        (tile for tile in tab_data["tiles"] if tile["name"] == "python_editor"),
        None,
    )
    assert editor_tile is not None
    assert "pandas" in editor_tile["editor_tile"]["content"]
    assert "analysis.py" == editor_tile["editor_tile"]["file_name"]


@pytest.mark.anyio
async def test_export_import_tab_template_with_valid_schema_roundtrip(
    client: AsyncClient,
):
    """Test exporting and then importing a tab template with valid schema (roundtrip)"""
    # Create interface and complex tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(
        client,
        interface_id,
        name="roundtrip_tab",
        color="#FF00FF",
        order=3,
    )
    tab_id = tab_response.json()["id"]

    # Add various tiles
    from orchestra.tests.test_tile import (
        _create_test_plot_tile,
        _create_test_table_tile,
        _create_test_terminal_tile,
    )

    await _create_test_table_tile(
        client,
        tab_id,
        name="roundtrip_table",
        table_type="advanced",
        selected="row1,row2",
    )
    await _create_test_plot_tile(
        client,
        tab_id,
        name="roundtrip_plot",
        plot_type="bar",
        x_axis="category",
        y_axis="count",
    )
    await _create_test_terminal_tile(
        client,
        tab_id,
        name="roundtrip_terminal",
        shell_type="zsh",
    )

    # Export the tab template
    export_request = {
        "tab_id": tab_id,
        "include_metadata": True,
        "description": "Roundtrip test tab",
        "tags": ["roundtrip", "test"],
    }

    export_response = await client.post(
        "/v0/tab/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert export_response.status_code == 200
    exported_template = export_response.json()["template"]

    # Delete the original tab
    await _delete_tab(client, tab_id=tab_id)

    # Import the template back to the same interface
    import_request = {
        "project": TEST_PROJECT,
        "template": exported_template,
        "interface_id": interface_id,
        "validate_first": False,
        "auto_sanitize": False,
    }

    import_response = await client.post(
        "/v0/tab/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert import_response.status_code == 200
    import_data = import_response.json()

    assert import_data["success"] is True
    assert import_data["import_stats"]["tabs"] == 1
    assert import_data["import_stats"]["tiles"] == 3

    # Verify the imported tab matches the original
    created_tab_id = import_data["created_ids"]["tab_id"]
    get_response = await _get_tab(client, tab_id=created_tab_id)
    imported_tab = get_response.json()

    assert imported_tab["name"] == "roundtrip_tab"
    assert imported_tab["color"] == "#FF00FF"
    assert imported_tab["order"] == 3
    assert len(imported_tab["tiles"]) == 3

    # Verify tile data was preserved
    tile_names = [tile["name"] for tile in imported_tab["tiles"]]
    assert "roundtrip_table" in tile_names
    assert "roundtrip_plot" in tile_names
    assert "roundtrip_terminal" in tile_names

    # Verify specialized data
    terminal_tile = next(
        (tile for tile in imported_tab["tiles"] if tile["type"] == "Terminal"),
        None,
    )
    assert terminal_tile is not None
    assert terminal_tile["terminal_tile"]["shell_type"] == "zsh"

    plot_tile = next(
        (tile for tile in imported_tab["tiles"] if tile["type"] == "Plot"),
        None,
    )
    assert plot_tile is not None
    assert plot_tile["plot_tile"]["plot_type"] == "bar"


@pytest.mark.anyio
async def test_import_tab_template_with_valid_schema_empty_template(
    client: AsyncClient,
):
    """Test importing tab template with valid schema containing empty template"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    empty_template = {
        "name": "empty_tab",
        "visible": True,
        "active": False,
        "order": 0,
        "tiles": [],
    }

    import_request = {
        "project": TEST_PROJECT,
        "template": empty_template,
        "interface_id": interface_id,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tab/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["tabs"] == 1
    assert data["import_stats"]["tiles"] == 0

    # Verify empty tab was created
    created_tab_id = data["created_ids"]["tab_id"]
    get_response = await _get_tab(client, tab_id=created_tab_id)
    tab_data = get_response.json()

    assert tab_data["name"] == "empty_tab"
    assert len(tab_data["tiles"]) == 0


@pytest.mark.anyio
async def test_export_tab_template_with_valid_schema_tile_positioning(
    client: AsyncClient,
):
    """Test exporting tab template with valid schema preserving tile positioning"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id, name="positioned_tab")
    tab_id = tab_response.json()["id"]

    # Create tiles with specific positions
    from orchestra.tests.test_tile import _create_test_tile

    await _create_test_tile(
        client,
        tab_id,
        name="top_left",
        x=0,
        y=0,
        width=4,
        height=3,
        tile_type="Table",
    )
    await _create_test_tile(
        client,
        tab_id,
        name="top_right",
        x=8,
        y=0,
        width=4,
        height=3,
        tile_type="Plot",
    )
    await _create_test_tile(
        client,
        tab_id,
        name="bottom_full",
        x=0,
        y=6,
        width=12,
        height=4,
        tile_type="Editor",
    )

    export_request = {
        "tab_id": tab_id,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/tab/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    template = response.json()["template"]

    # Verify positions are preserved
    tiles_by_name = {tile["name"]: tile for tile in template["tiles"]}

    assert tiles_by_name["top_left"]["position"]["x"] == 0
    assert tiles_by_name["top_left"]["position"]["y"] == 0
    assert tiles_by_name["top_left"]["position"]["width"] == 4
    assert tiles_by_name["top_left"]["position"]["height"] == 3

    assert tiles_by_name["top_right"]["position"]["x"] == 8
    assert tiles_by_name["top_right"]["position"]["y"] == 0

    assert tiles_by_name["bottom_full"]["position"]["width"] == 12
    assert tiles_by_name["bottom_full"]["position"]["height"] == 4


@pytest.mark.anyio
async def test_tab_context_validation(client: AsyncClient):
    """Test that tab API validates context references"""
    project_name = f"test-tab-context-{uuid.uuid4()}"
    context_name = "valid-context"
    invalid_context = "nonexistent-context"

    # Create project and context
    await _create_project(client, project_name)
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Valid context"},
        headers=HEADERS,
    )

    # Create interface
    interface_response = await _create_test_interface(
        client,
        name="test-interface",
        project=project_name,
    )
    interface_id = interface_response.json()["id"]

    # Create tab with valid context - should succeed
    response = await client.post(
        "/v0/tab/",
        json={
            "name": "valid-context-tab",
            "interface_id": interface_id,
            "context": context_name,
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["context"] == context_name

    # Try to create tab with invalid context - should fail
    response = await client.post(
        "/v0/tab/",
        json={
            "name": "invalid-context-tab",
            "interface_id": interface_id,
            "context": invalid_context,
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert f"Context '{invalid_context}' not found" in response.json()["detail"]

    # Clean up
    await _delete_project(client, project_name)
