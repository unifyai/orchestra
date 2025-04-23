import os
import pytest
import uuid
from httpx import AsyncClient

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
    response = await client.post("/v0/project", json={"name": project_name, "description": TEST_DESCRIPTION}, headers=HEADERS)
    assert response.status_code in [200, 201], f"Failed to create project: {response.json()}"
    return response


async def _delete_project(client: AsyncClient, project_name=TEST_PROJECT):
    """Delete a test project"""
    response = await client.delete(f"/v0/project/{project_name}", headers=HEADERS)
    return response


async def _create_test_interface(client: AsyncClient, name=TEST_INTERFACE, project_id=TEST_PROJECT, color="#FF0000"):
    """Create a test interface"""
    response = await client.post(
        "/v0/interfaces/",
        headers=HEADERS,
        json={"name": name, "project_id": project_id, "color": color, "description": TEST_DESCRIPTION},
    )
    assert response.status_code == 201, f"Failed to create interface: {response.json()}"
    return response


# Tab helpers
async def _create_test_tab(client: AsyncClient, interface_id, name=TEST_TAB, active=True, order=0):
    """Create a test tab"""
    response = await client.post(
        "/v0/tabs/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": name,
            "active": active,
            "order": order,
            "visible": True,
            "color": "#00FF00",
            "description": TEST_DESCRIPTION,
        },
    )
    assert response.status_code == 201, f"Failed to create tab: {response.json()}"
    return response


async def _get_tab(client: AsyncClient, tab_id=None, interface_id=None, name=None,
                 project_id=None, interface_name=None):
    """
    Get tab by ID or by interface_id+name or by project_id+interface_name+name
    
    If tab_id is provided, gets a single tab by ID
    If interface_id and name are provided, gets a single tab by interface_id and name
    If project_id, interface_name, and name are provided, gets a single tab by those parameters
    """
    if tab_id:
        return await client.get(f"/v0/tabs/{tab_id}", headers=HEADERS)
    elif interface_id and name:
        return await client.get(f"/v0/tabs/?interface_id={interface_id}&name={name}", headers=HEADERS)
    elif project_id and interface_name and name:
        url = f"/v0/tabs/?project_id={project_id}&interface_name={interface_name}&name={name}"
        return await client.get(url, headers=HEADERS)
    else:
        raise ValueError("Must provide either tab_id or interface_id+name or project_id+interface_name+name")


async def _list_tabs(client: AsyncClient, interface_id=None, project_id=None, interface_name=None, active=None):
    """List tabs for an interface"""
    params = {}
    if interface_id:
        params["interface_id"] = interface_id
    elif project_id and interface_name:
        params["project_id"] = project_id
        params["interface_name"] = interface_name
    else:
        raise ValueError("Must provide either interface_id or project_id+interface_name")
        
    if active is not None:
        params["active"] = str(active).lower()
        
    # Construct the URL with parameters
    param_str = "&".join([f"{k}={v}" for k, v in params.items()])
    return await client.get(f"/v0/tabs/list?{param_str}", headers=HEADERS)


async def _update_tab(client: AsyncClient, tab_id=None, interface_id=None, name=None,
                    project_id=None, interface_name=None, update_data=None):
    """Update tab by ID or by interface_id+name or by project_id+interface_name+name"""
    if update_data is None:
        update_data = {}
    
    if tab_id:
        return await client.put(f"/v0/tabs/{tab_id}", headers=HEADERS, json=update_data)
    elif interface_id and name:
        return await client.put(f"/v0/tabs/?interface_id={interface_id}&name={name}", headers=HEADERS, json=update_data)
    elif project_id and interface_name and name:
        url = f"/v0/tabs/?project_id={project_id}&interface_name={interface_name}&name={name}"
        return await client.put(url, headers=HEADERS, json=update_data)
    else:
        raise ValueError("Must provide either tab_id or interface_id+name or project_id+interface_name+name")


async def _delete_tab(client: AsyncClient, tab_id=None, interface_id=None, name=None,
                    project_id=None, interface_name=None):
    """Delete tab by ID or by interface_id+name or by project_id+interface_name+name"""
    if tab_id:
        return await client.delete(f"/v0/tabs/{tab_id}", headers=HEADERS)
    elif interface_id and name:
        return await client.delete(f"/v0/tabs/?interface_id={interface_id}&name={name}", headers=HEADERS)
    elif project_id and interface_name and name:
        url = f"/v0/tabs/?project_id={project_id}&interface_name={interface_name}&name={name}"
        return await client.delete(url, headers=HEADERS)
    else:
        raise ValueError("Must provide either tab_id or interface_id+name or project_id+interface_name+name")


async def _create_tab_checkpoint(client: AsyncClient, tab_id=None, interface_id=None, name=None,
                              project_id=None, interface_name=None):
    """Create a checkpoint for a tab"""
    if tab_id:
        return await client.post(f"/v0/tabs/{tab_id}/checkpoint", headers=HEADERS)
    elif interface_id and name:
        return await client.post(f"/v0/tabs/checkpoint?interface_id={interface_id}&name={name}", headers=HEADERS)
    elif project_id and interface_name and name:
        url = f"/v0/tabs/checkpoint?project_id={project_id}&interface_name={interface_name}&name={name}"
        return await client.post(url, headers=HEADERS)
    else:
        raise ValueError("Must provide either tab_id or interface_id+name or project_id+interface_name+name")


async def _get_tab_checkpoint(client: AsyncClient, tab_id=None, interface_id=None, name=None,
                             project_id=None, interface_name=None):
    """Get the latest checkpoint for a tab"""
    if tab_id:
        return await client.get(f"/v0/tabs/{tab_id}/checkpoint", headers=HEADERS)
    elif interface_id and name:
        return await client.get(f"/v0/tabs/checkpoint?interface_id={interface_id}&name={name}", headers=HEADERS)
    elif project_id and interface_name and name:
        url = f"/v0/tabs/checkpoint?project_id={project_id}&interface_name={interface_name}&name={name}"
        return await client.get(url, headers=HEADERS)
    else:
        raise ValueError("Must provide either tab_id or interface_id+name or project_id+interface_name+name")


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
    assert data["description"] == TEST_DESCRIPTION
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
async def test_get_tab_by_project_interface_and_name(client: AsyncClient):
    """Test getting a tab by project_id, interface_name, and name"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)
    
    # Get the tab by project_id, interface_name, and name
    response = await _get_tab(
        client, 
        project_id=TEST_PROJECT, 
        interface_name=TEST_INTERFACE,
        name=TEST_TAB
    )
    assert response.status_code == 200
    
    data = response.json()
    assert data["name"] == TEST_TAB


@pytest.mark.anyio
async def test_list_tabs_by_interface_id(client: AsyncClient):
    """Test listing tabs for an interface by interface_id"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    
    # Create multiple tabs
    await _create_test_tab(client, interface_id, name="list-tab-1", active=True, order=0)
    await _create_test_tab(client, interface_id, name="list-tab-2", active=False, order=1)
    
    # List tabs by interface_id
    response = await _list_tabs(client, interface_id=interface_id)
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 2
    tab_names = [tab["name"] for tab in data]
    assert "list-tab-1" in tab_names
    assert "list-tab-2" in tab_names


@pytest.mark.anyio
async def test_list_tabs_with_active_filter(client: AsyncClient):
    """Test listing tabs with active filter"""
    # Create an interface
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    
    # Create tabs with different active states
    await _create_test_tab(client, interface_id, name="active-tab", active=True)
    await _create_test_tab(client, interface_id, name="inactive-tab", active=False)
    
    # List only active tabs
    response = await _list_tabs(client, interface_id=interface_id, active=True)
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "active-tab"
    assert data[0]["active"] is True


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
    update_data = {
        "name": new_name,
        "visible": False,
        "color": "#0000FF",
        "description": "Updated description"
    }
    response = await _update_tab(client, tab_id=tab_id, update_data=update_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["id"] == tab_id
    assert data["name"] == new_name
    assert data["visible"] is False
    assert data["color"] == "#0000FF"
    assert data["description"] == "Updated description"


@pytest.mark.anyio
async def test_update_tab_by_interface_and_name(client: AsyncClient):
    """Test updating a tab by interface_id and name"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    await _create_test_tab(client, interface_id)
    
    # Update the tab by interface_id and name
    update_data = {
        "visible": False,
        "color": "#0000FF",
        "description": "Updated description"
    }
    response = await _update_tab(client, interface_id=interface_id, name=TEST_TAB, update_data=update_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["name"] == TEST_TAB
    assert data["visible"] is False
    assert data["color"] == "#0000FF"
    assert data["description"] == "Updated description"


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
    assert response.status_code == 204
    
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
    assert response.status_code == 204
    
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
    response = await _create_tab_checkpoint(client, interface_id=interface_id, name=TEST_TAB)
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
    tab1_response = await _create_test_tab(client, interface_id, name="active-tab", active=True)
    tab2_response = await _create_test_tab(client, interface_id, name="inactive-tab", active=False)
    
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
    
    # Verify only tab2 is active
    response = await _list_tabs(client, interface_id=interface_id, active=True)
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == tab2_id 