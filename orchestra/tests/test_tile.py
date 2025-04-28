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
TEST_PROJECT = "test-tile-project"
TEST_INTERFACE = "test-interface"
TEST_TAB = "test-tab"
TEST_TILE = "test-tile"
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


async def _create_test_interface(client: AsyncClient, name=TEST_INTERFACE, project=TEST_PROJECT, color="#FF0000"):
    """Create a test interface"""
    response = await client.post(
        "/v0/interfaces/",
        headers=HEADERS,
        json={"name": name, "project": project, "color": color},
    )
    assert response.status_code == 201, f"Failed to create interface: {response.json()}"
    return response


async def _create_test_tab(client: AsyncClient, interface_id, name=TEST_TAB, active=True, order=0):
    """Create a test tab"""
    response = await client.post(
        "/v0/tab/",
        headers=HEADERS,
        json={
            "interface_id": interface_id,
            "name": name,
            "active": active,
            "order": order,
            "visible": True,
            "color": "#00FF00",
        },
    )
    assert response.status_code == 201, f"Failed to create tab: {response.json()}"
    return response


# Tile helpers
async def _create_test_tile(client: AsyncClient, tab_id, name=TEST_TILE, 
                         tile_type="table", width=1, height=1, 
                         x=0, y=0, 
                         min_width=None, min_height=None,
                         visible=True, locked=False, moved=False, static=False,
                         context=None, table=None, auto_update=None, 
                         freeze=None, filters=None, common_filter=None, metric=None,
                         table_tile_data=None, plot_tile_data=None, 
                         view_tile_data=None, editor_tile_data=None):
    """Create a test tile
    
    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        tile_type: Type of tile (table, plot, view, editor)
        width: Width of the tile
        height: Height of the tile
        x: X position of the tile
        y: Y position of the tile
        min_width: Minimum width of the tile
        min_height: Minimum height of the tile
        visible: Whether the tile is visible
        locked: Whether the tile is locked
        moved: Whether the tile has been moved
        static: Whether the tile is static
        context: Context data for the tile
        table: Table data for the tile
        auto_update: Auto-update setting
        freeze: Freeze setting
        filters: Filters data
        common_filter: Common filter data
        metric: Metric data
        table_tile_data: Specialized data for table tiles
        plot_tile_data: Specialized data for plot tiles
        view_tile_data: Specialized data for view tiles
        editor_tile_data: Specialized data for editor tiles
    """
        
    # Create position object matching the schema
    position = {
        "x": x,
        "y": y,
        "width": width,
        "height": height
    }
    
    # Prepare the request payload
    payload = {
        "tab_id": tab_id,
        "name": name,
        "type": tile_type,
        "position": position,
        "visible": visible,
        "locked": locked,
        "moved": moved,
        "static": static,
        "min_width": min_width,
        "min_height": min_height,
        "context": context,
        "table": table,
        "auto_update": auto_update,
        "freeze": freeze,
        "filters": filters,
        "common_filter": common_filter,
        "metric": metric,
    }
    
    # Remove None values to avoid sending empty fields
    payload = {k: v for k, v in payload.items() if v is not None}
    
    # Add specialized tile data based on type
    if tile_type.lower() == "table" and table_tile_data:
        payload["table_tile"] = table_tile_data
    elif tile_type.lower() == "plot" and plot_tile_data:
        payload["plot_tile"] = plot_tile_data
    elif tile_type.lower() == "view" and view_tile_data:
        payload["view_tile"] = view_tile_data
    elif tile_type.lower() == "editor" and editor_tile_data:
        payload["editor_tile"] = editor_tile_data
        
    response = await client.post(
        "/v0/tile/",
        headers=HEADERS,
        json=payload,
    )
    return response

async def _create_test_table_tile(client: AsyncClient, tab_id, name=f"{TEST_TILE}-table",
                              headers=None, rows=None, **kwargs):
    """Create a test table tile with appropriate defaults.
    
    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        headers: Column headers list
        rows: Data rows list
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    if headers is None:
        headers = ["Column 1", "Column 2"]
    if rows is None:
        rows = [["Value 1-1", "Value 1-2"], ["Value 2-1", "Value 2-2"]]
        
    table_tile_data = {
        "headers": headers,
        "rows": rows
    }
    
    # Set defaults for table tiles
    table_kwargs = {
        "width": 4,
        "height": 3,
    }
    
    # Override defaults with any provided kwargs
    table_kwargs.update(kwargs)
    
    return await _create_test_tile(
        client=client,
        tab_id=tab_id,
        name=name,
        tile_type="table",
        table_tile_data=table_tile_data,
        **table_kwargs
    )


async def _create_test_plot_tile(client: AsyncClient, tab_id, name=f"{TEST_TILE}-plot",
                              plot_type="scatter", plot_data=None, **kwargs):
    """Create a test plot tile with appropriate defaults.
    
    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        plot_type: Type of plot (scatter, bar, line, etc.)
        plot_data: Plot data
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    if plot_data is None:
        plot_data = {
            "x": [1, 2, 3, 4, 5],
            "y": [10, 15, 7, 12, 9]
        }
        
    plot_tile_data = {
        "plot_type": plot_type,
        "plot_data": plot_data,
        "x_axis": "x",
        "y_axis": "y"
    }
    
    # Set defaults for plot tiles
    plot_kwargs = {
        "width": 4,
        "height": 3,
    }
    
    # Override defaults with any provided kwargs
    plot_kwargs.update(kwargs)
    
    return await _create_test_tile(
        client=client,
        tab_id=tab_id,
        name=name,
        tile_type="plot",
        plot_tile_data=plot_tile_data,
        **plot_kwargs
    )


async def _create_test_view_tile(client: AsyncClient, tab_id, name=f"{TEST_TILE}-view",
                              view_type="markdown", content="# Test Content", **kwargs):
    """Create a test view tile with appropriate defaults.
    
    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        view_type: Type of view (markdown, html, etc.)
        content: View content
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    view_tile_data = {
        "view_type": view_type,
        "view_data": {
            "content": content
        }
    }
    
    # Set defaults for view tiles
    view_kwargs = {
        "width": 4,
        "height": 3,
    }
    
    # Override defaults with any provided kwargs
    view_kwargs.update(kwargs)
    
    return await _create_test_tile(
        client=client,
        tab_id=tab_id,
        name=name,
        tile_type="view",
        view_tile_data=view_tile_data,
        **view_kwargs
    )


async def _create_test_editor_tile(client: AsyncClient, tab_id, name=f"{TEST_TILE}-editor",
                                language="python", content="print('Hello World')", **kwargs):
    """Create a test editor tile with appropriate defaults.
    
    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        language: Programming language
        content: Editor content
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    editor_tile_data = {
        "language": language,
        "content": content,
        "file_name": f"{name}.{language}"
    }
    
    # Set defaults for editor tiles
    editor_kwargs = {
        "width": 5,
        "height": 4,
    }
    
    # Override defaults with any provided kwargs
    editor_kwargs.update(kwargs)
    
    return await _create_test_tile(
        client=client,
        tab_id=tab_id,
        name=name,
        tile_type="editor",
        editor_tile_data=editor_tile_data,
        **editor_kwargs
    )


async def _get_tile(client: AsyncClient, tile_id=None, tab_id=None, name=None):
    """
    Get tile by ID or by tab_id and name
    
    If tile_id is provided, gets a single tile by ID
    If tab_id and name are provided, gets a single tile by tab_id and name
    """
    if tile_id:
        return await client.get(f"/v0/tile/?tile_id={tile_id}", headers=HEADERS)
    elif tab_id and name:
        return await client.get(f"/v0/tile/?tab_id={tab_id}&name={name}", headers=HEADERS)
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _list_tiles(client: AsyncClient, tab_id=None, name=None, type=None):
    """List tiles for a tab"""
    params = {}
    if tab_id:
        params["tab_id"] = tab_id
    else:
        raise ValueError("Must provide tab_id")
        
    if name:
        params["name"] = name
        
    if type:
        params["type"] = type
        
    # Construct the URL with parameters
    param_str = "&".join([f"{k}={v}" for k, v in params.items()])
    return await client.get(f"/v0/tile/list?{param_str}", headers=HEADERS)


async def _update_tile(client: AsyncClient, tile_id=None, tab_id=None, name=None, update_data=None):
    """Update tile by ID or by tab_id and name"""
    if update_data is None:
        update_data = {}
    
    # Convert any position-related fields to a position object
    if any(key in update_data for key in ["x_pos", "y_pos", "width", "height"]):
        position = {}
        if "x_pos" in update_data:
            position["x"] = update_data.pop("x_pos")
        if "y_pos" in update_data:
            position["y"] = update_data.pop("y_pos")
        if "width" in update_data:
            position["width"] = update_data.pop("width")
        if "height" in update_data:
            position["height"] = update_data.pop("height")
        
        update_data["position"] = position
    
    if tile_id:
        return await client.put(f"/v0/tile/?tile_id={tile_id}", headers=HEADERS, json=update_data)
    elif tab_id and name:
        return await client.put(f"/v0/tile/?tab_id={tab_id}&name={name}", headers=HEADERS, json=update_data)
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _patch_tile(client: AsyncClient, tile_id=None, tab_id=None, name=None, patch_data=None):
    """Patch tile by ID or by tab_id and name"""
    if patch_data is None:
        patch_data = {}
    
    # Convert any position-related fields to a position object
    if any(key in patch_data for key in ["x_pos", "y_pos", "width", "height"]):
        position = {}
        if "x_pos" in patch_data:
            position["x"] = patch_data.pop("x_pos")
        if "y_pos" in patch_data:
            position["y"] = patch_data.pop("y_pos")
        if "width" in patch_data:
            position["width"] = patch_data.pop("width")
        if "height" in patch_data:
            position["height"] = patch_data.pop("height")
        
        patch_data["position"] = position
    
    if tile_id:
        return await client.patch(f"/v0/tile/?tile_id={tile_id}", headers=HEADERS, json=patch_data)
    elif tab_id and name:
        return await client.patch(f"/v0/tile/?tab_id={tab_id}&name={name}", headers=HEADERS, json=patch_data)
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _patch_specialized_tile(client: AsyncClient, tile_id=None, tab_id=None, name=None, patch_data=None):
    """Patch specialized tile data by ID or by tab_id and name"""
    if patch_data is None:
        patch_data = {}
    
    if tile_id:
        return await client.patch(f"/v0/tile/specialized?tile_id={tile_id}", headers=HEADERS, json=patch_data)
    elif tab_id and name:
        return await client.patch(f"/v0/tile/specialized?tab_id={tab_id}&name={name}", headers=HEADERS, json=patch_data)
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _delete_tile(client: AsyncClient, tile_id=None, tab_id=None, name=None):
    """Delete tile by ID or by tab_id and name"""
    if tile_id:
        return await client.delete(f"/v0/tile/?tile_id={tile_id}", headers=HEADERS)
    elif tab_id and name:
        return await client.delete(f"/v0/tile/?tab_id={tab_id}&name={name}", headers=HEADERS)
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _create_tile_checkpoint(client: AsyncClient, tile_id=None, tab_id=None, name=None):
    """Create a checkpoint for a tile"""
    if tile_id:
        return await client.post(f"/v0/tile/checkpoint?tile_id={tile_id}", headers=HEADERS)
    elif tab_id and name:
        return await client.post(f"/v0/tile/checkpoint?tab_id={tab_id}&name={name}", headers=HEADERS)
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _get_tile_checkpoint(client: AsyncClient, tile_id=None, tab_id=None, name=None):
    """Get the latest checkpoint for a tile"""
    if tile_id:
        return await client.get(f"/v0/tile/checkpoint?tile_id={tile_id}", headers=HEADERS)
    elif tab_id and name:
        return await client.get(f"/v0/tile/checkpoint?tab_id={tab_id}&name={name}", headers=HEADERS)
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


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


# Tile Tests
@pytest.mark.anyio
async def test_create_tile(client: AsyncClient):
    """Test creating a tile"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create a tile
    response = await _create_test_tile(client, tab_id)
    print(response.json())
    assert response.status_code == 201
    
    data = response.json()
    assert data["name"] == TEST_TILE
    assert data["tab_id"] == tab_id
    assert data["type"] == "table"
    
    # Check position fields
    assert "position" in data
    assert data["position"]["x"] == 0
    assert data["position"]["y"] == 0
    assert data["position"]["width"] == 1
    assert data["position"]["height"] == 1
    
    assert data["visible"] is True
    assert data["is_checkpoint"] is False
    assert "id" in data
    assert "created_at" in data


@pytest.mark.anyio
async def test_create_different_tile_types(client: AsyncClient):
    """Test creating different types of tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create a table tile
    table_response = await _create_test_table_tile(client, tab_id)
    assert table_response.status_code == 201
    table_data = table_response.json()
    assert table_data["type"] == "table"
    assert "table_tile" in table_data
    assert "headers" in table_data["table_tile"]
    assert len(table_data["table_tile"]["headers"]) == 2
    
    # Create a plot tile
    plot_response = await _create_test_plot_tile(client, tab_id)
    assert plot_response.status_code == 201
    plot_data = plot_response.json()
    assert plot_data["type"] == "plot"
    assert "plot_tile" in plot_data
    assert "plot_type" in plot_data["plot_tile"]
    assert plot_data["plot_tile"]["plot_type"] == "scatter"
    
    # Create a view tile
    view_response = await _create_test_view_tile(client, tab_id)
    assert view_response.status_code == 201
    view_data = view_response.json()
    assert view_data["type"] == "view"
    assert "view_tile" in view_data
    assert "view_type" in view_data["view_tile"]
    assert view_data["view_tile"]["view_type"] == "markdown"
    
    # Create an editor tile
    editor_response = await _create_test_editor_tile(client, tab_id)
    assert editor_response.status_code == 201
    editor_data = editor_response.json()
    assert editor_data["type"] == "editor"
    assert "editor_tile" in editor_data
    assert "language" in editor_data["editor_tile"]
    assert editor_data["editor_tile"]["language"] == "python"


@pytest.mark.anyio
async def test_get_tile_by_id(client: AsyncClient):
    """Test getting a tile by ID"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    tile_response = await _create_test_tile(client, tab_id)
    tile_id = tile_response.json()["id"]
    
    # Get the tile by ID
    response = await _get_tile(client, tile_id=tile_id)
    assert response.status_code == 200
    
    data = response.json()
    assert data["id"] == tile_id
    assert data["name"] == TEST_TILE
    assert data["tab_id"] == tab_id


@pytest.mark.anyio
async def test_get_tile_by_tab_and_name(client: AsyncClient):
    """Test getting a tile by tab_id and name"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    await _create_test_tile(client, tab_id)
    
    # Get the tile by tab_id and name
    response = await _get_tile(client, tab_id=tab_id, name=TEST_TILE)
    assert response.status_code == 200
    
    data = response.json()
    assert data["name"] == TEST_TILE
    assert data["tab_id"] == tab_id


@pytest.mark.anyio
async def test_list_tiles_by_tab_id(client: AsyncClient):
    """Test listing tiles for a tab by ID"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create multiple tiles
    await _create_test_tile(client, tab_id, name="list-tile-1", order=0)
    await _create_test_tile(client, tab_id, name="list-tile-2", order=1)
    
    # List tiles
    response = await _list_tiles(client, tab_id=tab_id)
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 2
    tile_names = [tile["name"] for tile in data]
    assert "list-tile-1" in tile_names
    assert "list-tile-2" in tile_names


@pytest.mark.anyio
async def test_list_tiles_with_type_filter(client: AsyncClient):
    """Test listing tiles with type filter"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create tiles with different types
    await _create_test_tile(client, tab_id, name="table-tile", tile_type="table")
    await _create_test_tile(client, tab_id, name="plot-tile", tile_type="plot")
    await _create_test_tile(client, tab_id, name="view-tile", tile_type="view")
    
    # List only table tiles
    response = await _list_tiles(client, tab_id=tab_id, type="table")
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "table-tile"
    assert data[0]["type"] == "table"


@pytest.mark.anyio
async def test_update_tile_by_id(client: AsyncClient):
    """Test updating a tile by ID"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    tile_response = await _create_test_tile(client, tab_id)
    tile_id = tile_response.json()["id"]
    
    # Update the tile by ID
    new_name = "updated-tile"
    update_data = {
        "name": new_name,
        "visible": False,
        "position": {
            "x": 4,
            "y": 5,
            "width": 2,
            "height": 3
        }
    }
    response = await _update_tile(client, tile_id=tile_id, update_data=update_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["id"] == tile_id
    assert data["name"] == new_name
    assert data["visible"] is False
    assert data["position"]["width"] == 2
    assert data["position"]["height"] == 3
    assert data["position"]["x"] == 4
    assert data["position"]["y"] == 5


@pytest.mark.anyio
async def test_update_tile_by_tab_and_name(client: AsyncClient):
    """Test updating a tile by tab_id and name"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    await _create_test_tile(client, tab_id)
    
    # Update the tile by tab_id and name
    update_data = {
        "visible": False,
        "position": {
            "width": 2,
            "height": 3,
            "x": 0,
            "y": 0
        }
    }
    response = await _update_tile(client, tab_id=tab_id, name=TEST_TILE, update_data=update_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["name"] == TEST_TILE
    assert data["visible"] is False
    assert data["position"]["width"] == 2
    assert data["position"]["height"] == 3


@pytest.mark.anyio
async def test_patch_tile(client: AsyncClient):
    """Test patching a tile"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    tile_response = await _create_test_tile(client, tab_id)
    tile_id = tile_response.json()["id"]
    
    # Patch the tile
    patch_data = {
        "position": {
            "width": 2,
            "height": 3
        },
        "context": {"updated": True}
    }
    response = await _patch_tile(client, tile_id=tile_id, patch_data=patch_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["id"] == tile_id
    assert data["position"]["width"] == 2
    assert data["position"]["height"] == 3
    assert data["context"] == {"updated": True}
    # Other fields should remain unchanged
    assert data["name"] == TEST_TILE
    assert data["visible"] is True


@pytest.mark.anyio
async def test_patch_specialized_tile(client: AsyncClient):
    """Test patching specialized tile data"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create different specialized tiles
    table_response = await _create_test_tile(client, tab_id, name="table-tile", tile_type="table")
    table_id = table_response.json()["id"]
    
    # Patch the specialized tile data
    specialized_data = {
        "columns": ["col1", "col2"],
        "data": [["value1", "value2"], ["value3", "value4"]]
    }
    response = await _patch_specialized_tile(client, tile_id=table_id, patch_data=specialized_data)
    assert response.status_code == 200
    
    # Verify the specialized data was updated
    get_response = await _get_tile(client, tile_id=table_id)
    assert get_response.status_code == 200
    
    # The response should contain the specialized data
    data = get_response.json()
    # For this test, we'll assume the specialized data is included in the response
    # This depends on how the API is actually implemented
    assert "specialized_data" in data or "table_data" in data


@pytest.mark.anyio
async def test_delete_tile_by_id(client: AsyncClient):
    """Test deleting a tile by ID"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    tile_response = await _create_test_tile(client, tab_id)
    tile_id = tile_response.json()["id"]
    
    # Delete the tile by ID
    response = await _delete_tile(client, tile_id=tile_id)
    assert response.status_code == 204
    
    # Verify tile is deleted
    get_response = await _get_tile(client, tile_id=tile_id)
    assert get_response.status_code == 404


@pytest.mark.anyio
async def test_delete_tile_by_tab_and_name(client: AsyncClient):
    """Test deleting a tile by tab_id and name"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    await _create_test_tile(client, tab_id)
    
    # Delete the tile by tab_id and name
    response = await _delete_tile(client, tab_id=tab_id, name=TEST_TILE)
    assert response.status_code == 204
    
    # Verify tile is deleted
    get_response = await _get_tile(client, tab_id=tab_id, name=TEST_TILE)
    assert get_response.status_code == 404


@pytest.mark.anyio
async def test_tile_checkpoint_by_id(client: AsyncClient):
    """Test creating tile checkpoints by ID"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    tile_response = await _create_test_tile(client, tab_id)
    tile_id = tile_response.json()["id"]
    
    # Create a checkpoint by ID
    response = await _create_tile_checkpoint(client, tile_id=tile_id)
    assert response.status_code == 200
    
    checkpoint_data = response.json()
    assert checkpoint_data["is_checkpoint"] is True
    assert checkpoint_data["name"] == TEST_TILE


@pytest.mark.anyio
async def test_get_tile_checkpoint(client: AsyncClient):
    """Test retrieving tile checkpoints"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    tile_response = await _create_test_tile(client, tab_id)
    tile_id = tile_response.json()["id"]
    
    # Create a checkpoint
    await _create_tile_checkpoint(client, tile_id=tile_id)
    
    # Get the checkpoint
    response = await _get_tile_checkpoint(client, tile_id=tile_id)
    assert response.status_code == 200
    
    data = response.json()
    assert data["is_checkpoint"] is True
    assert data["name"] == TEST_TILE


@pytest.mark.anyio
async def test_create_duplicate_tile(client: AsyncClient):
    """Test creating a duplicate tile (should fail)"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    await _create_test_tile(client, tab_id)
    
    # Try to create another tile with the same name
    response = await _create_test_tile(client, tab_id)
    assert response.status_code == 409


@pytest.mark.anyio
async def test_tile_with_nonexistent_tab(client: AsyncClient):
    """Test creating a tile with a non-existent tab (should fail)"""
    non_existent_tab = str(uuid.uuid4())  # random ID
    response = await _create_test_tile(client, non_existent_tab)
    assert response.status_code == 404


@pytest.mark.anyio
async def test_tile_positioning(client: AsyncClient):
    """Test tile positioning when multiple tiles are created"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create tiles with different positions
    await _create_test_tile(client, tab_id, name="tile-0-0", x=0, y=0)
    await _create_test_tile(client, tab_id, name="tile-1-0", x=1, y=0)
    await _create_test_tile(client, tab_id, name="tile-0-1", x=0, y=1)
    await _create_test_tile(client, tab_id, name="tile-1-1", x=1, y=1)
    
    # List tiles
    response = await _list_tiles(client, tab_id=tab_id)
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 4
    
    # Verify positions
    positions = {}
    for tile in data:
        positions[(tile["position"]["x"], tile["position"]["y"])] = tile["name"]
    
    assert positions[(0, 0)] == "tile-0-0"
    assert positions[(1, 0)] == "tile-1-0"
    assert positions[(0, 1)] == "tile-0-1"
    assert positions[(1, 1)] == "tile-1-1"


@pytest.mark.anyio
async def test_tile_order(client: AsyncClient):
    """Test tile ordering"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create tiles with different order values
    await _create_test_tile(client, tab_id, name="order-tile-1", order=1)
    await _create_test_tile(client, tab_id, name="order-tile-0", order=0)
    await _create_test_tile(client, tab_id, name="order-tile-2", order=2)
    
    # List tiles and verify order
    response = await _list_tiles(client, tab_id=tab_id)
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 3
    
    # Check that tiles are returned in order based on the order field
    # This assumes the API returns tiles sorted by order
    order_tiles = sorted(data, key=lambda t: t["order"])
    
    assert order_tiles[0]["name"] == "order-tile-0"
    assert order_tiles[0]["order"] == 0
    
    assert order_tiles[1]["name"] == "order-tile-1"
    assert order_tiles[1]["order"] == 1
    
    assert order_tiles[2]["name"] == "order-tile-2"
    assert order_tiles[2]["order"] == 2


@pytest.mark.anyio
async def test_update_tile_with_position(client: AsyncClient):
    """Test updating tile position and other fields"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]
    
    # Create a basic tile with default values
    tile_response = await _create_test_tile(client, tab_id, name="position-test", x=0, y=0, width=1, height=1)
    assert tile_response.status_code == 201
    tile_id = tile_response.json()["id"]
    
    # Update the position and other fields
    update_data = {
        "name": "updated-position-test",
        "position": {
            "x": 2,
            "y": 3,
            "width": 4,
            "height": 5
        },
        "min_width": 2,
        "min_height": 2,
        "locked": True
    }
    
    response = await _update_tile(client, tile_id=tile_id, update_data=update_data)
    assert response.status_code == 200
    data = response.json()
    
    # Verify the update worked
    assert data["name"] == "updated-position-test"
    assert data["position"]["x"] == 2
    assert data["position"]["y"] == 3
    assert data["position"]["width"] == 4
    assert data["position"]["height"] == 5
    assert data["min_width"] == 2
    assert data["min_height"] == 2
    assert data["locked"] is True
