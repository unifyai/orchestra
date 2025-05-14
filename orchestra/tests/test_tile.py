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
TEST_PROJECT = "test-tile-project"
TEST_INTERFACE = "test-interface"
TEST_TAB = "test-tab"
TEST_TILE = "test-tile"


# Helper functions for project and interface creation
async def _create_project(client: AsyncClient, project_name=TEST_PROJECT):
    """Create a test project"""
    response = await client.post(
        "/v0/project",
        json={"name": project_name},
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


async def _create_test_tab(
    client: AsyncClient,
    interface_id,
    name=TEST_TAB,
    active=True,
    order=0,
):
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
async def _create_test_tile(
    client: AsyncClient,
    tab_id,
    name=TEST_TILE,
    tile_type=None,
    width=4,
    height=4,
    x=0,
    y=0,
    minW=None,
    minH=None,
    visible=True,
    locked=False,
    moved=False,
    static=False,
    color=None,
    context=None,
    table=None,
    auto_update=None,
    freeze=None,
    filters=None,
    common_filter=None,
    metric=None,
    column_context=None,
    grouping=None,
    table_tile_data=None,
    plot_tile_data=None,
    view_tile_data=None,
    editor_tile_data=None,
    tile_id=None,
):
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
        minW: Minimum width of the tile
        minH: Minimum height of the tile
        visible: Whether the tile is visible
        locked: Whether the tile is locked
        moved: Whether the tile has been moved
        static: Whether the tile is static
        color: Color of the tile
        context: Context data for the tile
        table: Table data for the tile
        auto_update: Auto-update setting
        freeze: Freeze setting
        filters: Filters data
        common_filter: Common filter data
        metric: Metric data
        column_context: Column context data
        grouping: Grouping data
        table_tile_data: Specialized data for table tiles
        plot_tile_data: Specialized data for plot tiles
        view_tile_data: Specialized data for view tiles
        editor_tile_data: Specialized data for editor tiles
        tile_id: Optional pre-specified ID for the tile
    """

    # Create position object matching the schema
    position = {"x": x, "y": y, "width": width, "height": height}

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
        "color": color,
        "minW": minW,
        "minH": minH,
        "context": context,
        "table": table,
        "auto_update": auto_update,
        "freeze": freeze,
        "filters": filters,
        "common_filter": common_filter,
        "metric": metric,
        "column_context": column_context,
        "grouping": grouping,
    }

    # Add the tile_id if provided
    if tile_id:
        payload["tile_id"] = tile_id

    # Remove None values to avoid sending empty fields
    payload = {k: v for k, v in payload.items() if v is not None}

    # Add specialized tile data based on type
    if tile_type and tile_type.lower() == "table" and table_tile_data:
        payload["table_tile"] = table_tile_data
    elif tile_type and tile_type.lower() == "plot" and plot_tile_data:
        payload["plot_tile"] = plot_tile_data
    elif tile_type and tile_type.lower() == "view" and view_tile_data:
        payload["view_tile"] = view_tile_data
    elif tile_type and tile_type.lower() == "editor" and editor_tile_data:
        payload["editor_tile"] = editor_tile_data

    response = await client.post(
        "/v0/tile/",
        headers=HEADERS,
        json=payload,
    )
    return response


async def _create_test_table_tile(
    client: AsyncClient,
    tab_id,
    name=f"{TEST_TILE}-table",
    table_type=None,
    page_number=None,
    column_order=None,
    hidden_columns=None,
    sorting=None,
    group_sorting=None,
    columns_pin_left=None,
    columns_pin_right=None,
    selected=None,
    **kwargs,
):
    """Create a test table tile with appropriate defaults.

    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        table_type: Type of table
        page_number: Page number
        column_order: Column order data
        hidden_columns: Hidden columns data
        sorting: Sorting data
        group_sorting: Group sorting data
        columns_pin_left: Columns pinned to left
        columns_pin_right: Columns pinned to right
        selected: Selected data
        **kwargs: Additional arguments to pass to _create_test_tile
    """

    table_tile_data = {
        "table_type": table_type,
        "page_number": page_number,
        "column_order": column_order,
        "hidden_columns": hidden_columns,
        "sorting": sorting,
        "group_sorting": group_sorting,
        "columns_pin_left": columns_pin_left,
        "columns_pin_right": columns_pin_right,
        "selected": selected,
    }

    # Remove None values
    table_tile_data = {k: v for k, v in table_tile_data.items()}

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
        tile_type="Table",
        table_tile_data=table_tile_data,
        **table_kwargs,
    )


async def _create_test_plot_tile(
    client: AsyncClient,
    tab_id,
    name=f"{TEST_TILE}-plot",
    plot_type="scatter",
    plot_scale_x=None,
    plot_scale_y=None,
    plot_aggregate=None,
    x_axis="x",
    y_axis="y",
    plot_group_by=None,
    plot_group_by_colors=None,
    bin_count=None,
    regression_line=None,
    **kwargs,
):
    """Create a test plot tile with appropriate defaults.

    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        plot_type: Type of plot (scatter, bar, line, etc.)
        plot_scale_x: X-axis scale
        plot_scale_y: Y-axis scale
        plot_aggregate: Plot aggregation
        x_axis: X-axis field
        y_axis: Y-axis field
        plot_group_by: Group by field
        plot_group_by_colors: Group by colors
        bin_count: Bin count for histograms
        regression_line: Regression line settings
        **kwargs: Additional arguments to pass to _create_test_tile
    """

    plot_tile_data = {
        "plot_type": plot_type,
        "plot_scale_x": plot_scale_x,
        "plot_scale_y": plot_scale_y,
        "plot_aggregate": plot_aggregate,
        "x_axis": x_axis,
        "y_axis": y_axis,
        "plot_group_by": plot_group_by,
        "plot_group_by_colors": plot_group_by_colors,
        "bin_count": bin_count,
        "regression_line": regression_line,
    }

    # Remove None values
    plot_tile_data = {k: v for k, v in plot_tile_data.items()}

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
        tile_type="Plot",
        plot_tile_data=plot_tile_data,
        **plot_kwargs,
    )


async def _create_test_view_tile(
    client: AsyncClient,
    tab_id,
    name=f"{TEST_TILE}-view",
    base_index=None,
    **kwargs,
):
    """Create a test view tile with appropriate defaults.

    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        base_index: Base index for the view
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    view_tile_data = {"base_index": base_index}

    # Remove None values
    view_tile_data = {k: v for k, v in view_tile_data.items()}

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
        tile_type="View",
        view_tile_data=view_tile_data,
        **view_kwargs,
    )


async def _create_test_editor_tile(
    client: AsyncClient,
    tab_id,
    name=f"{TEST_TILE}-editor",
    file_type="python",
    content="print('Hello World')",
    file_path=None,
    **kwargs,
):
    """Create a test editor tile with appropriate defaults.

    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        file_type: File type/language
        content: Editor content
        file_path: Path of the file
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    # Default file name if not provided
    if file_path is None:
        file_path = f"{name}.{file_type}"

    editor_tile_data = {
        "file_path": file_path,
        "file_type": file_type,
        "content": content,
    }

    # Remove None values
    editor_tile_data = {k: v for k, v in editor_tile_data.items()}

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
        tile_type="Editor",
        editor_tile_data=editor_tile_data,
        **editor_kwargs,
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
        return await client.get(
            f"/v0/tile/?tab_id={tab_id}&name={name}",
            headers=HEADERS,
        )
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


async def _update_tile(
    client: AsyncClient,
    tile_id=None,
    tab_id=None,
    name=None,
    update_data=None,
):
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
        return await client.put(
            f"/v0/tile/?tile_id={tile_id}",
            headers=HEADERS,
            json=update_data,
        )
    elif tab_id and name:
        return await client.put(
            f"/v0/tile/?tab_id={tab_id}&name={name}",
            headers=HEADERS,
            json=update_data,
        )
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _patch_tile(
    client: AsyncClient,
    tile_id=None,
    tab_id=None,
    name=None,
    patch_data=None,
):
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
        return await client.patch(
            f"/v0/tile/?tile_id={tile_id}",
            headers=HEADERS,
            json=patch_data,
        )
    elif tab_id and name:
        return await client.patch(
            f"/v0/tile/?tab_id={tab_id}&name={name}",
            headers=HEADERS,
            json=patch_data,
        )
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _patch_specialized_tile(
    client: AsyncClient,
    tile_type=None,
    tile_id=None,
    tab_id=None,
    name=None,
    patch_data=None,
):
    """Patch specialized tile data by ID or by tab_id and name

    Args:
        client: AsyncClient for making requests
        tile_type: The type of tile (Table, Plot, View, Editor)
        tile_id: ID of the tile to patch
        tab_id: ID of the tab containing the tile
        name: Name of the tile to patch
        patch_data: Data to patch in the specialized tile
    """
    if patch_data is None:
        patch_data = {}

    params = {}
    if tile_type:
        params["tile_type"] = tile_type

    if tile_id:
        params["tile_id"] = tile_id
    elif tab_id and name:
        params["tab_id"] = tab_id
        params["name"] = name
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")

    # Construct the URL with parameters
    param_str = "&".join([f"{k}={v}" for k, v in params.items()])
    return await client.patch(
        f"/v0/tile/specialized?{param_str}",
        headers=HEADERS,
        json=patch_data,
    )


async def _delete_tile(client: AsyncClient, tile_id=None, tab_id=None, name=None):
    """Delete tile by ID or by tab_id and name"""
    if tile_id:
        return await client.delete(f"/v0/tile/?tile_id={tile_id}", headers=HEADERS)
    elif tab_id and name:
        return await client.delete(
            f"/v0/tile/?tab_id={tab_id}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _create_tile_checkpoint(
    client: AsyncClient,
    tile_id=None,
    tab_id=None,
    name=None,
):
    """Create a checkpoint for a tile"""
    if tile_id:
        return await client.post(
            f"/v0/tile/checkpoint?tile_id={tile_id}",
            headers=HEADERS,
        )
    elif tab_id and name:
        return await client.post(
            f"/v0/tile/checkpoint?tab_id={tab_id}&name={name}",
            headers=HEADERS,
        )
    else:
        raise ValueError("Must provide either tile_id or tab_id+name")


async def _get_tile_checkpoint(
    client: AsyncClient,
    tile_id=None,
    tab_id=None,
    name=None,
):
    """Get the latest checkpoint for a tile"""
    if tile_id:
        return await client.get(
            f"/v0/tile/checkpoint?tile_id={tile_id}",
            headers=HEADERS,
        )
    elif tab_id and name:
        return await client.get(
            f"/v0/tile/checkpoint?tab_id={tab_id}&name={name}",
            headers=HEADERS,
        )
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
    assert response.status_code == 201

    data = response.json()
    assert data["name"] == TEST_TILE
    assert data["tab_id"] == tab_id

    # Check position fields
    assert "position" in data
    assert data["position"]["x"] == 0
    assert data["position"]["y"] == 0
    assert data["position"]["width"] == 4
    assert data["position"]["height"] == 4

    assert data["visible"] is True
    assert data["is_checkpoint"] is False
    assert "id" in data
    assert "created_at" in data


@pytest.mark.anyio
async def test_create_tile_with_color(client: AsyncClient):
    """Test creating a tile with color"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a tile
    response = await _create_test_tile(
        client,
        tab_id,
        name="color-tile",
        color="#FF0000",
    )
    assert response.status_code == 201

    data = response.json()
    assert data["name"] == "color-tile"
    assert data["tab_id"] == tab_id

    # Check color field
    assert data["color"] == "#FF0000"


@pytest.mark.anyio
async def test_create_different_tile_types(client: AsyncClient):
    """Test creating different types of tiles"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a table tile
    table_response = await _create_test_table_tile(
        client,
        tab_id,
        table_type="basic",
        selected="123,456",
        page_number="1",
    )
    assert table_response.status_code == 201
    table_data = table_response.json()
    assert table_data["type"] == "Table"
    assert "table_tile" in table_data
    assert table_data["table_tile"]["table_type"] == "basic"
    assert table_data["table_tile"]["selected"] == "123,456"
    assert table_data["table_tile"]["page_number"] == "1"
    # Verify specialized tile ID relationship
    assert "id" in table_data["table_tile"]
    assert table_data["table_tile"]["id"] is not None
    assert table_data["table_tile"]["id"] != table_data["id"]
    assert table_data["table_tile"]["tile_id"] == table_data["id"]

    # Create a plot tile
    plot_response = await _create_test_plot_tile(
        client,
        tab_id,
        plot_type="scatter",
        x_axis="x_data",
        y_axis="y_data",
    )
    assert plot_response.status_code == 201
    plot_data = plot_response.json()
    assert plot_data["type"] == "Plot"
    assert "plot_tile" in plot_data
    assert plot_data["plot_tile"]["plot_type"] == "scatter"
    assert plot_data["plot_tile"]["x_axis"] == "x_data"
    assert plot_data["plot_tile"]["y_axis"] == "y_data"
    # Verify specialized tile ID relationship
    assert "id" in plot_data["plot_tile"]
    assert plot_data["plot_tile"]["id"] is not None
    assert plot_data["plot_tile"]["id"] != plot_data["id"]
    assert plot_data["plot_tile"]["tile_id"] == plot_data["id"]

    # Create a view tile
    view_response = await _create_test_view_tile(client, tab_id, base_index="index1")
    assert view_response.status_code == 201
    view_data = view_response.json()
    assert view_data["type"] == "View"
    assert "view_tile" in view_data
    assert view_data["view_tile"]["base_index"] == "index1"
    # Verify specialized tile ID relationship
    assert "id" in view_data["view_tile"]
    assert view_data["view_tile"]["id"] is not None
    assert view_data["view_tile"]["id"] != view_data["id"]
    assert view_data["view_tile"]["tile_id"] == view_data["id"]

    # Create an editor tile
    editor_response = await _create_test_editor_tile(
        client,
        tab_id,
        file_type="python",
        content="print('Test')",
        file_path="test.py",
    )
    assert editor_response.status_code == 201
    editor_data = editor_response.json()
    assert editor_data["type"] == "Editor"
    assert "editor_tile" in editor_data
    assert editor_data["editor_tile"]["file_type"] == "python"
    assert editor_data["editor_tile"]["content"] == "print('Test')"
    assert editor_data["editor_tile"]["file_path"] == "test.py"
    # Verify specialized tile ID relationship
    assert "id" in editor_data["editor_tile"]
    assert editor_data["editor_tile"]["id"] is not None
    assert editor_data["editor_tile"]["id"] != editor_data["id"]
    assert editor_data["editor_tile"]["tile_id"] == editor_data["id"]


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
    await _create_test_tile(client, tab_id, name="list-tile-1")
    await _create_test_tile(client, tab_id, name="list-tile-2")

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
    await _create_test_tile(client, tab_id, name="table-tile", tile_type="Table")
    await _create_test_tile(client, tab_id, name="plot-tile", tile_type="Plot")
    await _create_test_tile(client, tab_id, name="view-tile", tile_type="View")

    # List only table tiles
    response = await _list_tiles(client, tab_id=tab_id, type="Table")
    assert response.status_code == 200

    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "table-tile"
    assert data[0]["type"] == "Table"


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
        "position": {"x": 4, "y": 5, "width": 2, "height": 3},
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
        "position": {"width": 2, "height": 3, "x": 0, "y": 0},
    }
    response = await _update_tile(
        client,
        tab_id=tab_id,
        name=TEST_TILE,
        update_data=update_data,
    )
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
        "position": {"width": 2, "height": 3},
        "context": "Parameters/student/student_id",
    }
    response = await _patch_tile(client, tile_id=tile_id, patch_data=patch_data)
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == tile_id
    assert data["position"]["width"] == 2
    assert data["position"]["height"] == 3
    assert data["context"] == "Parameters/student/student_id"
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
    table_response = await _create_test_table_tile(client, tab_id, name="table-tile")
    table_id = table_response.json()["id"]

    # Patch the specialized tile data
    specialized_data = {"columns_pin_left": ["RowNumbering"], "page_number": "1"}
    response = await _patch_specialized_tile(
        client,
        tile_type="Table",
        tile_id=table_id,
        patch_data=specialized_data,
    )
    assert response.status_code == 200

    # Verify the specialized data was updated
    get_response = await _get_tile(client, tile_id=table_id)
    assert get_response.status_code == 200

    # The response should contain the specialized data
    data = get_response.json()
    assert data["table_tile"]["columns_pin_left"] == '["RowNumbering"]'
    assert data["table_tile"]["page_number"] == "1"


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
async def test_update_tile_with_position(client: AsyncClient):
    """Test updating tile position and other fields"""
    # Create an interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a basic tile with default values
    tile_response = await _create_test_tile(
        client,
        tab_id,
        name="position-test",
        x=0,
        y=0,
        width=1,
        height=1,
    )
    assert tile_response.status_code == 201
    tile_id = tile_response.json()["id"]

    # Update the position and other fields
    update_data = {
        "name": "updated-position-test",
        "position": {"x": 2, "y": 3, "width": 4, "height": 5},
        "minW": 2,
        "minH": 2,
        "locked": True,
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
    assert data["minW"] == 2
    assert data["minH"] == 2
    assert data["locked"] is True


@pytest.mark.anyio
async def test_update_specialized_tile_data(client: AsyncClient):
    """Test updating specialized tile data"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a table tile with initial data
    table_tile_response = await _create_test_table_tile(
        client,
        tab_id,
        name="specialized-table",
        table_type="basic",
        selected="Entries/id_123456,Entries/id_123457",
    )
    table_tile_id = table_tile_response.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in table_tile_response.json()["table_tile"]
    assert table_tile_response.json()["table_tile"]["id"] is not None
    assert table_tile_response.json()["table_tile"]["id"] != table_tile_id
    assert table_tile_response.json()["table_tile"]["tile_id"] == table_tile_id

    # Update the specialized table data
    update_data = {
        "table_tile": {
            "table_type": "advanced",
            "selected": "Entries/id_123458,Entries/id_123459",
            "sorting": "column1:asc",
        },
    }
    response = await _patch_tile(client, tile_id=table_tile_id, patch_data=update_data)
    assert response.status_code == 200

    # Verify the update worked
    updated_data = response.json()
    assert updated_data["table_tile"]["table_type"] == "advanced"
    assert (
        updated_data["table_tile"]["selected"] == "Entries/id_123458,Entries/id_123459"
    )
    assert updated_data["table_tile"]["sorting"] == "column1:asc"
    # Verify specialized tile ID relationship after update
    assert "id" in updated_data["table_tile"]
    assert updated_data["table_tile"]["id"] is not None
    assert updated_data["table_tile"]["id"] != updated_data["id"]
    assert updated_data["table_tile"]["tile_id"] == updated_data["id"]

    # Create a plot tile
    plot_tile_response = await _create_test_plot_tile(
        client,
        tab_id,
        name="specialized-plot",
        plot_type="scatter",
    )
    plot_tile_id = plot_tile_response.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in plot_tile_response.json()["plot_tile"]
    assert plot_tile_response.json()["plot_tile"]["id"] is not None
    assert plot_tile_response.json()["plot_tile"]["id"] != plot_tile_id
    assert plot_tile_response.json()["plot_tile"]["tile_id"] == plot_tile_id

    # Update the specialized plot data
    update_data = {
        "plot_tile": {
            "plot_type": "bar",
            "plot_scale_x": "linear",
            "plot_scale_y": "log",
            "x_axis": "category",
            "y_axis": "value",
        },
    }
    response = await _patch_tile(client, tile_id=plot_tile_id, patch_data=update_data)
    assert response.status_code == 200

    # Verify the update worked
    updated_data = response.json()
    assert updated_data["plot_tile"]["plot_type"] == "bar"
    assert updated_data["plot_tile"]["plot_scale_x"] == "linear"
    assert updated_data["plot_tile"]["plot_scale_y"] == "log"
    assert updated_data["plot_tile"]["x_axis"] == "category"
    assert updated_data["plot_tile"]["y_axis"] == "value"
    # Verify specialized tile ID relationship after update
    assert "id" in updated_data["plot_tile"]
    assert updated_data["plot_tile"]["id"] is not None
    assert updated_data["plot_tile"]["id"] != updated_data["id"]
    assert updated_data["plot_tile"]["tile_id"] == updated_data["id"]

    # Create an editor tile
    editor_tile_response = await _create_test_editor_tile(
        client,
        tab_id,
        name="specialized-editor",
        file_type="python",
    )
    editor_tile_id = editor_tile_response.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in editor_tile_response.json()["editor_tile"]
    assert editor_tile_response.json()["editor_tile"]["id"] is not None
    assert editor_tile_response.json()["editor_tile"]["id"] != editor_tile_id
    assert editor_tile_response.json()["editor_tile"]["tile_id"] == editor_tile_id

    # Update the specialized editor data
    update_data = {
        "editor_tile": {
            "file_type": "javascript",
            "file_path": "script.js",
            "content": "console.log('Hello');",
        },
    }
    response = await _patch_tile(client, tile_id=editor_tile_id, patch_data=update_data)
    assert response.status_code == 200

    # Verify the update worked
    updated_data = response.json()
    assert updated_data["editor_tile"]["file_type"] == "javascript"
    assert updated_data["editor_tile"]["file_path"] == "script.js"
    assert updated_data["editor_tile"]["content"] == "console.log('Hello');"
    # Verify specialized tile ID relationship after update
    assert "id" in updated_data["editor_tile"]
    assert updated_data["editor_tile"]["id"] is not None
    assert updated_data["editor_tile"]["id"] != updated_data["id"]
    assert updated_data["editor_tile"]["tile_id"] == updated_data["id"]


@pytest.mark.anyio
async def test_patch_specialized_tile_endpoint(client: AsyncClient):
    """Test the specialized patch endpoint for each tile type"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create tiles of each type
    table_tile = await _create_test_table_tile(client, tab_id, name="spec-table-tile")
    print(table_tile.json())
    table_id = table_tile.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in table_tile.json()["table_tile"]
    assert table_tile.json()["table_tile"]["id"] is not None
    assert table_tile.json()["table_tile"]["id"] != table_id
    assert table_tile.json()["table_tile"]["tile_id"] == table_id

    plot_tile = await _create_test_plot_tile(client, tab_id, name="spec-plot-tile")
    plot_id = plot_tile.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in plot_tile.json()["plot_tile"]
    assert plot_tile.json()["plot_tile"]["id"] is not None
    assert plot_tile.json()["plot_tile"]["id"] != plot_id
    assert plot_tile.json()["plot_tile"]["tile_id"] == plot_id

    view_tile = await _create_test_view_tile(client, tab_id, name="spec-view-tile")
    view_id = view_tile.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in view_tile.json()["view_tile"]
    assert view_tile.json()["view_tile"]["id"] is not None
    assert view_tile.json()["view_tile"]["id"] != view_id
    assert view_tile.json()["view_tile"]["tile_id"] == view_id

    editor_tile = await _create_test_editor_tile(
        client,
        tab_id,
        name="spec-editor-tile",
    )
    editor_id = editor_tile.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in editor_tile.json()["editor_tile"]
    assert editor_tile.json()["editor_tile"]["id"] is not None
    assert editor_tile.json()["editor_tile"]["id"] != editor_id
    assert editor_tile.json()["editor_tile"]["tile_id"] == editor_id

    # Test patch for table tile
    table_patch = {
        "table_type": "specialized-type",
        "selected": "Entries/id_123460,Entries/id_123461",
        "sorting": "field:desc",
    }
    table_response = await _patch_specialized_tile(
        client,
        tile_type="Table",
        tile_id=table_id,
        patch_data=table_patch,
    )
    assert table_response.status_code == 200
    table_data = table_response.json()
    assert table_data["table_tile"]["table_type"] == "specialized-type"
    assert table_data["table_tile"]["selected"] == "Entries/id_123460,Entries/id_123461"
    assert table_data["table_tile"]["sorting"] == "field:desc"
    # Verify specialized tile ID relationship after patch
    assert "id" in table_data["table_tile"]
    assert table_data["table_tile"]["id"] is not None
    assert table_data["table_tile"]["id"] != table_data["id"]
    assert table_data["table_tile"]["tile_id"] == table_data["id"]

    # Test patch for plot tile
    plot_patch = {
        "plot_type": "bar",
        "plot_scale_x": "continuous",
        "plot_aggregate": "sum",
    }
    plot_response = await _patch_specialized_tile(
        client,
        tile_type="Plot",
        tile_id=plot_id,
        patch_data=plot_patch,
    )
    assert plot_response.status_code == 200
    plot_data = plot_response.json()
    assert plot_data["plot_tile"]["plot_type"] == "bar"
    assert plot_data["plot_tile"]["plot_scale_x"] == "continuous"
    assert plot_data["plot_tile"]["plot_aggregate"] == "sum"
    # Verify specialized tile ID relationship after patch
    assert "id" in plot_data["plot_tile"]
    assert plot_data["plot_tile"]["id"] is not None
    assert plot_data["plot_tile"]["id"] != plot_data["id"]
    assert plot_data["plot_tile"]["tile_id"] == plot_data["id"]

    # Test patch for view tile
    view_patch = {"base_index": "updated-index"}
    view_response = await _patch_specialized_tile(
        client,
        tile_type="View",
        tile_id=view_id,
        patch_data=view_patch,
    )
    assert view_response.status_code == 200
    view_data = view_response.json()
    assert view_data["view_tile"]["base_index"] == "updated-index"
    # Verify specialized tile ID relationship after patch
    assert "id" in view_data["view_tile"]
    assert view_data["view_tile"]["id"] is not None
    assert view_data["view_tile"]["id"] != view_data["id"]
    assert view_data["view_tile"]["tile_id"] == view_data["id"]

    # Test patch for editor tile
    editor_patch = {
        "file_path": "updated.js",
        "file_type": "javascript",
        "content": "console.log('Updated');",
    }
    editor_response = await _patch_specialized_tile(
        client,
        tile_type="Editor",
        tile_id=editor_id,
        patch_data=editor_patch,
    )
    assert editor_response.status_code == 200
    editor_data = editor_response.json()
    assert editor_data["editor_tile"]["file_path"] == "updated.js"
    assert editor_data["editor_tile"]["file_type"] == "javascript"
    assert editor_data["editor_tile"]["content"] == "console.log('Updated');"
    # Verify specialized tile ID relationship after patch
    assert "id" in editor_data["editor_tile"]
    assert editor_data["editor_tile"]["id"] is not None
    assert editor_data["editor_tile"]["id"] != editor_data["id"]
    assert editor_data["editor_tile"]["tile_id"] == editor_data["id"]


@pytest.mark.anyio
async def test_create_tile_with_specified_id(client: AsyncClient):
    """Test creating a tile with a user-specified ID"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Generate a UUID to use for the tile
    specified_id = str(uuid.uuid4())

    # Create a tile with the specified ID
    response = await _create_test_tile(
        client,
        tab_id,
        name="predetermined-id-tile",
        tile_id=specified_id,
    )
    assert response.status_code == 201

    data = response.json()
    assert data["id"] == specified_id
    assert data["name"] == "predetermined-id-tile"
    assert data["tab_id"] == tab_id

    # Verify we can retrieve the tile by its ID
    get_response = await _get_tile(client, tile_id=specified_id)
    assert get_response.status_code == 200
    get_data = get_response.json()
    assert get_data["id"] == specified_id


@pytest.mark.anyio
async def test_create_tile_with_no_type(client: AsyncClient):
    """Test creating a tile with no type"""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Generate a UUID to use for the tile
    specified_id = str(uuid.uuid4())

    # Create a tile with the specified ID
    response = await _create_test_tile(
        client,
        tab_id,
        name="no-type-tile",
        tile_id=specified_id,
    )
    assert response.status_code == 201

    data = response.json()
    assert data["id"] == specified_id
    assert data["name"] == "no-type-tile"
    assert data["tab_id"] == tab_id

    # Verify we can retrieve the tile by its ID
    get_response = await _get_tile(client, tile_id=specified_id)
    assert get_response.status_code == 200
    get_data = get_response.json()
    assert get_data["id"] == specified_id


@pytest.mark.anyio
async def test_patch_tile_set_type_and_specialized_data(client: AsyncClient):
    """Create tile with no type then patch to Table with specialized data."""
    # Create interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create tile without specifying type
    create_response = await _create_test_tile(
        client,
        tab_id,
        name="set-type-table-tile",
    )
    assert create_response.status_code == 201
    tile_id = create_response.json()["id"]

    # Patch to set type Table with specialized payload
    table_payload = {
        "type": "Table",
        "table_tile": {
            "table_type": "basic",
            "page_number": "1",
        },
    }
    patch_response = await _patch_tile(
        client,
        tile_id=tile_id,
        patch_data=table_payload,
    )
    assert patch_response.status_code == 200

    data = patch_response.json()
    assert data["type"] == "Table"
    assert data["table_tile"] is not None
    assert data["table_tile"]["table_type"] == "basic"
    # Other specialized tiles should be None
    assert data["plot_tile"] is None
    assert data["view_tile"] is None
    assert data["editor_tile"] is None
    # Verify specialized tile ID relationship
    assert "id" in data["table_tile"]
    assert data["table_tile"]["id"] is not None
    assert data["table_tile"]["id"] != data["id"]
    assert data["table_tile"]["tile_id"] == data["id"]

    # Retrieve and double-check
    get_resp = await _get_tile(client, tile_id=tile_id)
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["type"] == "Table"
    assert get_data["table_tile"]["table_type"] == "basic"
    # Verify specialized tile ID relationship again
    assert get_data["table_tile"]["id"] != get_data["id"]
    assert get_data["table_tile"]["tile_id"] == get_data["id"]


@pytest.mark.anyio
async def test_patch_tile_change_type_with_new_specialized_data(client: AsyncClient):
    """Change tile type from Table to Plot and verify specialized objects shift."""
    # Interface and tab
    interface_resp = await _create_test_interface(client)
    tab_resp = await _create_test_tab(client, interface_resp.json()["id"])
    tab_id = tab_resp.json()["id"]

    # Create initial Table tile
    create_resp = await _create_test_table_tile(
        client,
        tab_id,
        name="change-to-plot-tile",
        table_type="initial",
    )
    assert create_resp.status_code == 201
    tile_id = create_resp.json()["id"]
    # Verify initial specialized tile ID relationship
    assert "id" in create_resp.json()["table_tile"]
    assert create_resp.json()["table_tile"]["id"] is not None
    assert create_resp.json()["table_tile"]["id"] != tile_id
    assert create_resp.json()["table_tile"]["tile_id"] == tile_id

    # Patch to switch to Plot with payload
    plot_payload = {
        "type": "Plot",
        "plot_tile": {
            "plot_type": "scatter",
            "x_axis": "x_data",
            "y_axis": "y_data",
        },
    }
    patch_resp = await _patch_tile(client, tile_id=tile_id, patch_data=plot_payload)
    assert patch_resp.status_code == 200
    data = patch_resp.json()
    assert data["type"] == "Plot"
    assert data["plot_tile"] is not None
    assert data["plot_tile"]["plot_type"] == "scatter"
    # Table specialized should now be None
    assert data["table_tile"] is None
    assert data["view_tile"] is None
    assert data["editor_tile"] is None
    # Verify specialized tile ID relationship
    assert "id" in data["plot_tile"]
    assert data["plot_tile"]["id"] is not None
    assert data["plot_tile"]["id"] != data["id"]
    assert data["plot_tile"]["tile_id"] == data["id"]

    # Retrieve again
    get_r = await _get_tile(client, tile_id=tile_id)
    assert get_r.status_code == 200
    get_d = get_r.json()
    assert get_d["type"] == "Plot"
    assert get_d["plot_tile"]["x_axis"] == "x_data"
    assert get_d["table_tile"] is None
    # Verify specialized tile ID relationship again
    assert get_d["plot_tile"]["id"] != get_d["id"]
    assert get_d["plot_tile"]["tile_id"] == get_d["id"]


@pytest.mark.anyio
async def test_patch_tile_change_type_without_specialized_data(client: AsyncClient):
    """Change type without supplying specialized payload to ensure defaults created."""
    interface_resp = await _create_test_interface(client)
    tab_resp = await _create_test_tab(client, interface_resp.json()["id"])
    tab_id = tab_resp.json()["id"]

    # Create Editor tile first
    editor_resp = await _create_test_editor_tile(
        client,
        tab_id,
        name="to-view-default",
    )
    assert editor_resp.status_code == 201
    tile_id = editor_resp.json()["id"]
    # Verify initial specialized tile ID relationship
    assert "id" in editor_resp.json()["editor_tile"]
    assert editor_resp.json()["editor_tile"]["id"] is not None
    assert editor_resp.json()["editor_tile"]["id"] != tile_id
    assert editor_resp.json()["editor_tile"]["tile_id"] == tile_id

    # Change type to View without view_tile data
    patch_payload = {"type": "View"}
    patch_resp = await _patch_tile(client, tile_id=tile_id, patch_data=patch_payload)
    assert patch_resp.status_code == 200
    data = patch_resp.json()
    assert data["type"] == "View"
    # View tile should exist even though payload not provided (default)
    assert data["view_tile"] is not None
    # Other specialized tiles None
    assert data["table_tile"] is None
    assert data["plot_tile"] is None
    assert data["editor_tile"] is None
    # Verify specialized tile ID relationship
    assert "id" in data["view_tile"]
    assert data["view_tile"]["id"] is not None
    assert data["view_tile"]["id"] != data["id"]
    assert data["view_tile"]["tile_id"] == data["id"]


@pytest.mark.anyio
async def test_create_tile_with_specialized_data_in_one_step(client: AsyncClient):
    """Test creating a tile with specialized data in a single DAO call."""
    # Create an interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]
    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create tiles of each type with specialized data in one step

    # Table tile
    table_response = await _create_test_tile(
        client,
        tab_id,
        name="one-step-table-tile",
        tile_type="Table",
        table_tile_data={
            "table_type": "unified",
            "page_number": "1",
            "column_order": "id,name,value",
        },
    )
    assert table_response.status_code == 201
    table_data = table_response.json()
    assert table_data["type"] == "Table"
    assert table_data["table_tile"] is not None
    assert table_data["table_tile"]["table_type"] == "unified"
    assert table_data["table_tile"]["page_number"] == "1"
    assert table_data["table_tile"]["column_order"] == "id,name,value"
    # Verify specialized tile ID relationship
    assert "id" in table_data["table_tile"]
    assert table_data["table_tile"]["id"] is not None
    assert table_data["table_tile"]["id"] != table_data["id"]
    assert table_data["table_tile"]["tile_id"] == table_data["id"]

    # Plot tile
    plot_response = await _create_test_tile(
        client,
        tab_id,
        name="one-step-plot-tile",
        tile_type="Plot",
        plot_tile_data={
            "plot_type": "scatter",
            "x_axis": "time",
            "y_axis": "value",
            "plot_scale_x": "linear",
            "plot_scale_y": "log",
        },
    )
    assert plot_response.status_code == 201
    plot_data = plot_response.json()
    assert plot_data["type"] == "Plot"
    assert plot_data["plot_tile"] is not None
    assert plot_data["plot_tile"]["plot_type"] == "scatter"
    assert plot_data["plot_tile"]["x_axis"] == "time"
    assert plot_data["plot_tile"]["plot_scale_y"] == "log"
    # Verify specialized tile ID relationship
    assert "id" in plot_data["plot_tile"]
    assert plot_data["plot_tile"]["id"] is not None
    assert plot_data["plot_tile"]["id"] != plot_data["id"]
    assert plot_data["plot_tile"]["tile_id"] == plot_data["id"]

    # View tile
    view_response = await _create_test_tile(
        client,
        tab_id,
        name="one-step-view-tile",
        tile_type="View",
        view_tile_data={
            "base_index": "custom-index",
        },
    )
    assert view_response.status_code == 201
    view_data = view_response.json()
    assert view_data["type"] == "View"
    assert view_data["view_tile"] is not None
    assert view_data["view_tile"]["base_index"] == "custom-index"
    # Verify specialized tile ID relationship
    assert "id" in view_data["view_tile"]
    assert view_data["view_tile"]["id"] is not None
    assert view_data["view_tile"]["id"] != view_data["id"]
    assert view_data["view_tile"]["tile_id"] == view_data["id"]

    # Editor tile
    editor_response = await _create_test_tile(
        client,
        tab_id,
        name="one-step-editor-tile",
        tile_type="Editor",
        editor_tile_data={
            "file_type": "markdown",
            "file_path": "notes.md",
            "content": "# Test Notes\nThis is a test document.",
        },
    )
    assert editor_response.status_code == 201
    editor_data = editor_response.json()
    assert editor_data["type"] == "Editor"
    assert editor_data["editor_tile"] is not None
    assert editor_data["editor_tile"]["file_type"] == "markdown"
    assert (
        editor_data["editor_tile"]["content"]
        == "# Test Notes\nThis is a test document."
    )
    # Verify specialized tile ID relationship
    assert "id" in editor_data["editor_tile"]
    assert editor_data["editor_tile"]["id"] is not None
    assert editor_data["editor_tile"]["id"] != editor_data["id"]
    assert editor_data["editor_tile"]["tile_id"] == editor_data["id"]

    # Test with pre-specified ID
    custom_id = str(uuid.uuid4())
    custom_id_response = await _create_test_tile(
        client,
        tab_id,
        name="custom-id-table-tile",
        tile_type="Table",
        table_tile_data={
            "table_type": "custom-id-table",
        },
        tile_id=custom_id,
    )
    assert custom_id_response.status_code == 201
    custom_id_data = custom_id_response.json()
    assert custom_id_data["id"] == custom_id
    assert custom_id_data["table_tile"] is not None
    assert custom_id_data["table_tile"]["tile_id"] == custom_id
    # Verify specialized tile ID relationship for pre-specified ID
    assert "id" in custom_id_data["table_tile"]
    assert custom_id_data["table_tile"]["id"] is not None
    assert custom_id_data["table_tile"]["id"] != custom_id
    assert custom_id_data["table_tile"]["tile_id"] == custom_id
