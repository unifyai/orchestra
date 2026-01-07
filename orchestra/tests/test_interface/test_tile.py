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
        json={"name": name, "project_name": project, "color": color},
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
    terminal_tile_data=None,
    tile_id=None,
):
    """Create a test tile

    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        tile_type: Type of tile (table, plot, view, editor, terminal)
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
        terminal_tile_data: Specialized data for terminal tiles
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
    elif tile_type and tile_type.lower() == "terminal" and terminal_tile_data:
        payload["terminal_tile"] = terminal_tile_data

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
    default_hidden_columns=None,
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
        default_hidden_columns: Default hidden columns boolean
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
        "default_hidden_columns": default_hidden_columns,
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
    file_name=None,
    **kwargs,
):
    """Create a test editor tile with appropriate defaults.

    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        file_type: File type/language
        content: Editor content
        file_name: Name of the file
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    # Default file name if not provided
    if file_name is None:
        file_name = f"{name}.{file_type}"

    editor_tile_data = {
        "file_name": file_name,
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


async def _create_test_terminal_tile(
    client: AsyncClient,
    tab_id,
    name=f"{TEST_TILE}-terminal",
    shell_type="bash",
    **kwargs,
):
    """Create a test terminal tile with appropriate defaults.

    Args:
        client: AsyncClient for making requests
        tab_id: ID of the tab to create the tile in
        name: Name of the tile
        shell_type: Shell type for the terminal
        **kwargs: Additional arguments to pass to _create_test_tile
    """
    terminal_tile_data = {"shell_type": shell_type}

    # Remove None values
    terminal_tile_data = {k: v for k, v in terminal_tile_data.items()}

    # Set defaults for terminal tiles
    terminal_kwargs = {
        "width": 4,
        "height": 3,
    }

    # Override defaults with any provided kwargs
    terminal_kwargs.update(kwargs)

    return await _create_test_tile(
        client=client,
        tab_id=tab_id,
        name=name,
        tile_type="Terminal",
        terminal_tile_data=terminal_tile_data,
        **terminal_kwargs,
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
        tile_type: The type of tile (Table, Plot, View, Editor, Terminal)
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
        file_name="test.py",
    )
    assert editor_response.status_code == 201
    editor_data = editor_response.json()
    assert editor_data["type"] == "Editor"
    assert "editor_tile" in editor_data
    assert editor_data["editor_tile"]["file_type"] == "python"
    assert editor_data["editor_tile"]["content"] == "print('Test')"
    assert editor_data["editor_tile"]["file_name"] == "test.py"
    # Verify specialized tile ID relationship
    assert "id" in editor_data["editor_tile"]
    assert editor_data["editor_tile"]["id"] is not None
    assert editor_data["editor_tile"]["id"] != editor_data["id"]
    assert editor_data["editor_tile"]["tile_id"] == editor_data["id"]

    # Create a terminal tile
    terminal_response = await _create_test_terminal_tile(
        client,
        tab_id,
        shell_type="bash",
    )
    assert terminal_response.status_code == 201
    terminal_data = terminal_response.json()
    assert terminal_data["type"] == "Terminal"
    assert "terminal_tile" in terminal_data
    assert terminal_data["terminal_tile"]["shell_type"] == "bash"
    # Verify specialized tile ID relationship
    assert "id" in terminal_data["terminal_tile"]
    assert terminal_data["terminal_tile"]["id"] is not None
    assert terminal_data["terminal_tile"]["id"] != terminal_data["id"]
    assert terminal_data["terminal_tile"]["tile_id"] == terminal_data["id"]


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
        "context": None,  # Use null instead of non-existent context
    }
    response = await _patch_tile(client, tile_id=tile_id, patch_data=patch_data)
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == tile_id
    assert data["position"]["width"] == 2
    assert data["position"]["height"] == 3
    assert data["context"] is None  # Should be null
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
    assert response.status_code == 200

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
    assert response.status_code == 200

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
            "file_name": "script.js",
            "content": "console.log('Hello');",
        },
    }
    response = await _patch_tile(client, tile_id=editor_tile_id, patch_data=update_data)
    assert response.status_code == 200

    # Verify the update worked
    updated_data = response.json()
    assert updated_data["editor_tile"]["file_type"] == "javascript"
    assert updated_data["editor_tile"]["file_name"] == "script.js"
    assert updated_data["editor_tile"]["content"] == "console.log('Hello');"
    # Verify specialized tile ID relationship after update
    assert "id" in updated_data["editor_tile"]
    assert updated_data["editor_tile"]["id"] is not None
    assert updated_data["editor_tile"]["id"] != updated_data["id"]
    assert updated_data["editor_tile"]["tile_id"] == updated_data["id"]

    # Create a terminal tile
    terminal_tile_response = await _create_test_terminal_tile(
        client,
        tab_id,
        shell_type="bash",
    )
    assert terminal_tile_response.status_code == 201
    terminal_tile_id = terminal_tile_response.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in terminal_tile_response.json()["terminal_tile"]
    assert terminal_tile_response.json()["terminal_tile"]["id"] is not None
    assert terminal_tile_response.json()["terminal_tile"]["id"] != terminal_tile_id
    assert terminal_tile_response.json()["terminal_tile"]["tile_id"] == terminal_tile_id

    # Update the specialized terminal data
    update_data = {
        "terminal_tile": {
            "shell_type": "zsh",
        },
    }
    response = await _patch_tile(
        client,
        tile_id=terminal_tile_id,
        patch_data=update_data,
    )
    assert response.status_code == 200
    updated_data = response.json()
    assert updated_data["terminal_tile"]["shell_type"] == "zsh"
    # Verify specialized tile ID relationship after update
    assert "id" in updated_data["terminal_tile"]
    assert updated_data["terminal_tile"]["id"] is not None
    assert updated_data["terminal_tile"]["id"] != updated_data["id"]
    assert updated_data["terminal_tile"]["tile_id"] == updated_data["id"]


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

    terminal_tile = await _create_test_terminal_tile(
        client,
        tab_id,
        name="spec-terminal-tile",
    )
    terminal_id = terminal_tile.json()["id"]
    # Verify specialized tile ID relationship
    assert "id" in terminal_tile.json()["terminal_tile"]
    assert terminal_tile.json()["terminal_tile"]["id"] is not None
    assert terminal_tile.json()["terminal_tile"]["id"] != terminal_id
    assert terminal_tile.json()["terminal_tile"]["tile_id"] == terminal_id

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
        "file_name": "updated.js",
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
    assert editor_data["editor_tile"]["file_name"] == "updated.js"
    assert editor_data["editor_tile"]["file_type"] == "javascript"
    assert editor_data["editor_tile"]["content"] == "console.log('Updated');"
    # Verify specialized tile ID relationship after patch
    assert "id" in editor_data["editor_tile"]
    assert editor_data["editor_tile"]["id"] is not None
    assert editor_data["editor_tile"]["id"] != editor_data["id"]
    assert editor_data["editor_tile"]["tile_id"] == editor_data["id"]

    # Test patch for terminal tile
    terminal_patch = {
        "shell_type": "zsh",
    }
    terminal_response = await _patch_specialized_tile(
        client,
        tile_type="Terminal",
        tile_id=terminal_id,
        patch_data=terminal_patch,
    )
    assert terminal_response.status_code == 200
    terminal_data = terminal_response.json()
    assert terminal_data["terminal_tile"]["shell_type"] == "zsh"
    # Verify specialized tile ID relationship after patch
    assert "id" in terminal_data["terminal_tile"]
    assert terminal_data["terminal_tile"]["id"] is not None
    assert terminal_data["terminal_tile"]["id"] != terminal_data["id"]
    assert terminal_data["terminal_tile"]["tile_id"] == terminal_data["id"]


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
    assert data["terminal_tile"] is None
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
    assert data["terminal_tile"] is None
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
    assert data["terminal_tile"] is None
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
            "file_name": "notes.md",
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

    # Create a terminal tile
    terminal_reponse = await _create_test_tile(
        client,
        tab_id,
        name="one-step-terminal-tile",
        tile_type="Terminal",
        terminal_tile_data={"shell_type": "bash"},
    )
    assert terminal_reponse.status_code == 201
    terminal_data = terminal_reponse.json()
    assert terminal_data["type"] == "Terminal"
    assert terminal_data["terminal_tile"] is not None
    assert terminal_data["terminal_tile"]["shell_type"] == "bash"
    # Verify specialized tile ID relationship
    assert "id" in terminal_data["terminal_tile"]
    assert terminal_data["terminal_tile"]["id"] is not None
    assert terminal_data["terminal_tile"]["id"] != terminal_data["id"]
    assert terminal_data["terminal_tile"]["tile_id"] == terminal_data["id"]

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


@pytest.mark.anyio
async def test_restore_tile_checkpoint(client: AsyncClient):
    """Ensure a tile checkpoint keeps its original state even after the active tile and its specialized data are updated."""
    # Create interface and tab
    interface_resp = await _create_test_interface(client)
    interface_id = interface_resp.json()["id"]
    tab_resp = await _create_test_tab(client, interface_id)
    tab_id = tab_resp.json()["id"]

    # Create a Table tile with initial properties
    original_color = "#123456"
    table_resp = await _create_test_table_tile(
        client,
        tab_id,
        name="checkpoint-table-tile",
        table_type="basic",
        color=original_color,
    )
    assert table_resp.status_code == 201
    tile_id = table_resp.json()["id"]

    print(table_resp.json())

    # Create checkpoint
    cp_resp = await _create_tile_checkpoint(client, tile_id=tile_id)
    assert cp_resp.status_code == 200
    cp_id = cp_resp.json()["id"]  # may be needed for debugging
    assert cp_resp.json()["is_checkpoint"] is True
    assert cp_resp.json()["color"] == original_color
    assert cp_resp.json()["table_tile"]["table_type"] == "basic"

    # Update active tile (both base and specialized)
    patch_payload = {
        "color": "#654321",
        "table_tile": {
            "table_type": "advanced",
        },
    }
    upd_resp = await _patch_tile(client, tile_id=tile_id, patch_data=patch_payload)
    assert upd_resp.status_code == 200
    assert upd_resp.json()["color"] == "#654321"
    assert upd_resp.json()["table_tile"]["table_type"] == "advanced"

    # Fetch checkpoint again and assert immutability
    cp_fetch = await _get_tile_checkpoint(client, tile_id=tile_id)
    assert cp_fetch.status_code == 200
    cp_data = cp_fetch.json()

    assert cp_data["color"] == original_color  # original color retained
    assert cp_data["table_tile"]["table_type"] == "basic"  # original specialized data
    assert cp_data["is_checkpoint"] is True

    # Active tile should reflect updated values
    active_get = await _get_tile(client, tile_id=tile_id)
    assert active_get.status_code == 200
    active_data = active_get.json()
    assert active_data["color"] == "#654321"
    assert active_data["table_tile"]["table_type"] == "advanced"


@pytest.mark.anyio
async def test_export_tile_template_with_valid_schema(client: AsyncClient):
    """Test exporting a tile template with valid schema"""
    # Create interface, tab, and tile
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id, name="export_tile_tab")
    tab_id = tab_response.json()["id"]

    # Create a complex table tile
    tile_response = await _create_test_table_tile(
        client,
        tab_id,
        name="export_table_tile",
        table_type="advanced",
        page_number="3",
        column_order='["id", "name", "value", "timestamp"]',
        hidden_columns='["internal_id", "temp_field"]',
        default_hidden_columns=False,
        sorting='{"timestamp": "desc", "value": "asc"}',
        group_sorting='{"category": "asc"}',
        columns_pin_left='["id", "name"]',
        columns_pin_right='["timestamp"]',
        selected="row_1,row_3,row_7",
        width=8,
        height=6,
        x=2,
        y=1,
        color="#FF0000",
        visible=True,
        locked=False,
    )
    tile_id = tile_response.json()["id"]

    # Export tile template
    export_request = {
        "tile_id": tile_id,
        "include_metadata": True,
        "description": "Test table tile template",
        "tags": ["test", "table", "tile"],
        "template_name": "Advanced Table Template",
    }

    response = await client.post(
        "/v0/tile/export_template",
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
    assert template["name"] == "export_table_tile"
    assert template["type"] == "Table"
    assert template["position"]["x"] == 2
    assert template["position"]["y"] == 1
    assert template["position"]["width"] == 8
    assert template["position"]["height"] == 6
    assert template["color"] == "#FF0000"
    assert template["visible"] is True
    assert template["locked"] is False

    # Verify specialized table data
    assert "table_tile" in template
    table_data = template["table_tile"]
    assert table_data["table_type"] == "advanced"
    assert table_data["page_number"] == "3"
    assert "id" in table_data["column_order"]
    assert "internal_id" in table_data["hidden_columns"]
    assert table_data["default_hidden_columns"] is False
    assert table_data["selected"] == "row_1,row_3,row_7"

    # Verify export stats
    stats = data["export_stats"]
    assert stats["tiles"] == 1


@pytest.mark.anyio
async def test_export_tile_template_with_valid_schema_plot_tile(client: AsyncClient):
    """Test exporting a plot tile template with valid schema"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a complex plot tile
    tile_response = await _create_test_plot_tile(
        client,
        tab_id,
        name="export_plot_tile",
        plot_type="scatter",
        plot_scale_x="log",
        plot_scale_y="linear",
        plot_aggregate="mean",
        x_axis="timestamp",
        y_axis="value",
        plot_group_by="category",
        plot_group_by_colors='{"A": "#FF0000", "B": "#00FF00", "C": "#0000FF"}',
        bin_count="25",
        regression_line="true",
        width=6,
        height=4,
        x=0,
        y=0,
    )
    tile_id = tile_response.json()["id"]

    export_request = {
        "tile_id": tile_id,
        "include_metadata": True,
        "description": "Complex scatter plot template",
    }

    response = await client.post(
        "/v0/tile/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    template = response.json()["template"]

    assert template["name"] == "export_plot_tile"
    assert template["type"] == "Plot"

    # Verify plot-specific data
    plot_data = template["plot_tile"]
    assert plot_data["plot_type"] == "scatter"
    assert plot_data["plot_scale_x"] == "log"
    assert plot_data["plot_scale_y"] == "linear"
    assert plot_data["x_axis"] == "timestamp"
    assert plot_data["y_axis"] == "value"
    assert plot_data["regression_line"] == "true"
    assert "A" in plot_data["plot_group_by_colors"]


@pytest.mark.anyio
async def test_export_tile_template_with_valid_schema_by_tab_and_name(
    client: AsyncClient,
):
    """Test exporting tile template with valid schema using tab_id and name"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    await _create_test_editor_tile(
        client,
        tab_id,
        name="named_editor_tile",
        file_type="python",
        content="print('export by name test')",
        file_name="test_script.py",
    )

    export_request = {
        "tab_id": tab_id,
        "tile_name": "named_editor_tile",
        "include_metadata": True,
        "description": "Export by tab and name",
    }

    response = await client.post(
        "/v0/tile/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    template = response.json()["template"]

    print(response.json())

    assert template["name"] == "named_editor_tile"
    assert template["type"] == "Editor"
    assert template["description"] == "Export by tab and name"
    assert "export by name test" in template["editor_tile"]["content"]


@pytest.mark.anyio
async def test_export_tile_template_with_valid_schema_checkpoint(client: AsyncClient):
    """Test exporting tile template with valid schema from checkpoint"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create tile
    tile_response = await _create_test_view_tile(
        client,
        tab_id,
        name="checkpoint_view_tile",
        base_index="markdown",
    )
    tile_id = tile_response.json()["id"]

    # Create checkpoint
    checkpoint_response = await _create_tile_checkpoint(client, tile_id=tile_id)
    assert checkpoint_response.status_code == 200

    # Update original tile after checkpoint
    await _update_tile(
        client,
        tile_id=tile_id,
        update_data={"view_tile": {"base_index": "html"}},
    )

    # Export from checkpoint
    checkpoint_tile_id = checkpoint_response.json()["id"]
    export_request = {
        "tile_id": checkpoint_tile_id,
        "checkpoint": True,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/tile/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    template = response.json()["template"]

    # Should export checkpoint version (original base_index)
    assert template["view_tile"]["base_index"] == "markdown"


@pytest.mark.anyio
async def test_export_tile_template_with_valid_schema_all_tile_types(
    client: AsyncClient,
):
    """Test exporting templates for all tile types with valid schema"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Test each tile type
    tile_configs = [
        {
            "create_func": _create_test_table_tile,
            "kwargs": {"name": "table_export", "table_type": "basic"},
            "expected_type": "Table",
            "specialized_field": "table_tile",
        },
        {
            "create_func": _create_test_plot_tile,
            "kwargs": {"name": "plot_export", "plot_type": "bar"},
            "expected_type": "Plot",
            "specialized_field": "plot_tile",
        },
        {
            "create_func": _create_test_view_tile,
            "kwargs": {"name": "view_export", "base_index": "json"},
            "expected_type": "View",
            "specialized_field": "view_tile",
        },
        {
            "create_func": _create_test_editor_tile,
            "kwargs": {
                "name": "editor_export",
                "file_type": "sql",
                "content": "SELECT * FROM table;",
            },
            "expected_type": "Editor",
            "specialized_field": "editor_tile",
        },
        {
            "create_func": _create_test_terminal_tile,
            "kwargs": {"name": "terminal_export", "shell_type": "zsh"},
            "expected_type": "Terminal",
            "specialized_field": "terminal_tile",
        },
    ]

    for config in tile_configs:
        # Create tile
        tile_response = await config["create_func"](client, tab_id, **config["kwargs"])
        tile_id = tile_response.json()["id"]

        # Export template
        export_request = {
            "tile_id": tile_id,
            "include_metadata": True,
        }

        response = await client.post(
            "/v0/tile/export_template",
            json=export_request,
            headers=HEADERS,
        )

        assert response.status_code == 200
        template = response.json()["template"]

        assert template["name"] == config["kwargs"]["name"]
        assert template["type"] == config["expected_type"]
        assert config["specialized_field"] in template


@pytest.mark.anyio
async def test_import_tile_template_with_valid_schema(client: AsyncClient):
    """Test importing a tile template with valid schema"""
    # Create target tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create a valid template
    template = {
        "name": "imported_table_tile",
        "position": {"x": 4, "y": 2, "width": 8, "height": 6},
        "type": "Table",
        "visible": True,
        "locked": False,
        "moved": False,
        "static": False,
        "color": "#FF00FF",
        "table_tile": {
            "table_type": "advanced",
            "page_number": "2",
            "column_order": '["id", "name", "status"]',
            "hidden_columns": '["internal_notes"]',
            "default_hidden_columns": False,
            "sorting": '{"name": "asc"}',
            "group_sorting": '{"status": "desc"}',
            "columns_pin_left": '["id"]',
            "columns_pin_right": '["status"]',
            "selected": "row_2,row_4,row_6",
        },
    }

    import_request = {
        "project_name": TEST_PROJECT,
        "template": template,
        "tab_id": tab_id,
        "validate_first": False,  # Skip validation for v0
        "auto_sanitize": False,
        "overwrite_existing": False,
    }

    response = await client.post(
        "/v0/tile/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["tiles"] == 1

    # Verify the tile was created
    created_tile_id = data["created_ids"]["tile_id"]
    get_response = await _get_tile(client, tile_id=created_tile_id)
    assert get_response.status_code == 200

    tile_data = get_response.json()
    assert tile_data["name"] == "imported_table_tile"
    assert tile_data["type"] == "Table"
    assert tile_data["position"]["x"] == 4
    assert tile_data["position"]["y"] == 2
    assert tile_data["position"]["width"] == 8
    assert tile_data["position"]["height"] == 6
    assert tile_data["color"] == "#FF00FF"

    # Verify specialized table data was preserved
    table_data = tile_data["table_tile"]
    assert table_data["table_type"] == "advanced"
    assert table_data["page_number"] == "2"
    assert "id" in table_data["column_order"]
    assert "internal_notes" in table_data["hidden_columns"]
    assert table_data["default_hidden_columns"] is False
    assert table_data["selected"] == "row_2,row_4,row_6"


@pytest.mark.anyio
async def test_import_tile_template_with_valid_schema_new_name(client: AsyncClient):
    """Test importing tile template with valid schema and new name override"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    template = {
        "name": "original_tile_name",
        "position": {"x": 0, "y": 0, "width": 4, "height": 3},
        "type": "Plot",
        "plot_tile": {
            "plot_type": "line",
            "x_axis": "time",
            "y_axis": "value",
        },
    }

    import_request = {
        "project_name": TEST_PROJECT,
        "template": template,
        "tab_id": tab_id,
        "new_tile_name": "overridden_tile_name",
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tile/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    # Verify tile was created with new name
    created_tile_id = data["created_ids"]["tile_id"]
    get_response = await _get_tile(client, tile_id=created_tile_id)
    tile_data = get_response.json()
    assert tile_data["name"] == "overridden_tile_name"


@pytest.mark.anyio
async def test_import_tile_template_with_valid_schema_by_interface_and_tab_name(
    client: AsyncClient,
):
    """Test importing tile template with valid schema using interface_id and tab_name"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    await _create_test_tab(client, interface_id, name="target_tab")

    template = {
        "name": "imported_by_tab_name",
        "position": {"x": 0, "y": 0, "width": 4, "height": 3},
        "type": "Editor",
        "editor_tile": {
            "file_type": "javascript",
            "content": "console.log('imported by tab name');",
        },
    }

    import_request = {
        "project_name": TEST_PROJECT,
        "template": template,
        "interface_id": interface_id,
        "tab_name": "target_tab",
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tile/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["tiles"] == 1


@pytest.mark.anyio
async def test_import_tile_template_with_valid_schema_overwrite_existing(
    client: AsyncClient,
):
    """Test importing tile template with valid schema and overwrite existing"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create existing tile
    existing_tile_response = await _create_test_table_tile(
        client,
        tab_id,
        name="existing_tile",
        table_type="basic",
    )

    template = {
        "name": "existing_tile",
        "position": {"x": 0, "y": 0, "width": 6, "height": 4},
        "type": "Table",
        "table_tile": {
            "table_type": "advanced",  # Different type
            "page_number": "3",
        },
    }

    # First try without overwrite
    import_request = {
        "project_name": TEST_PROJECT,
        "template": template,
        "tab_id": tab_id,
        "overwrite_existing": False,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tile/import_template",
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
        "/v0/tile/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
async def test_import_tile_template_with_valid_schema_all_tile_types(
    client: AsyncClient,
):
    """Test importing templates for all tile types with valid schema"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Test templates for each tile type
    templates = [
        {
            "name": "imported_table",
            "position": {"x": 0, "y": 0, "width": 6, "height": 4},
            "type": "Table",
            "table_tile": {
                "table_type": "advanced",
                "page_number": "1",
                "column_order": '["col1", "col2"]',
            },
        },
        {
            "name": "imported_plot",
            "position": {"x": 6, "y": 0, "width": 6, "height": 4},
            "type": "Plot",
            "plot_tile": {
                "plot_type": "histogram",
                "x_axis": "data",
                "bin_count": "20",
            },
        },
        {
            "name": "imported_view",
            "position": {"x": 0, "y": 4, "width": 4, "height": 3},
            "type": "View",
            "view_tile": {
                "base_index": "csv",
            },
        },
        {
            "name": "imported_editor",
            "position": {"x": 4, "y": 4, "width": 4, "height": 3},
            "type": "Editor",
            "editor_tile": {
                "file_type": "r",
                "file_name": "analysis.R",
                "content": "# R analysis script\ndata <- read.csv('data.csv')\nsummary(data)",
            },
        },
        {
            "name": "imported_terminal",
            "position": {"x": 8, "y": 4, "width": 4, "height": 3},
            "type": "Terminal",
            "terminal_tile": {
                "shell_type": "fish",
            },
        },
    ]

    created_tile_ids = []

    for template in templates:
        import_request = {
            "project_name": TEST_PROJECT,
            "template": template,
            "tab_id": tab_id,
            "validate_first": False,
            "auto_sanitize": False,
        }

        response = await client.post(
            "/v0/tile/import_template",
            json=import_request,
            headers=HEADERS,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        created_tile_ids.append(data["created_ids"]["tile_id"])

    # Verify all tiles were created correctly
    assert len(created_tile_ids) == 5

    for i, tile_id in enumerate(created_tile_ids):
        get_response = await _get_tile(client, tile_id=tile_id)
        tile_data = get_response.json()

        expected_template = templates[i]
        assert tile_data["name"] == expected_template["name"]
        assert tile_data["type"] == expected_template["type"]

        # Verify specialized data
        if expected_template["type"] == "Editor":
            assert "R analysis script" in tile_data["editor_tile"]["content"]
        elif expected_template["type"] == "Terminal":
            assert tile_data["terminal_tile"]["shell_type"] == "fish"


@pytest.mark.anyio
async def test_import_tile_template_with_valid_schema_complex_positioning(
    client: AsyncClient,
):
    """Test importing tile template with valid schema and complex positioning"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    template = {
        "name": "positioned_tile",
        "position": {"x": 3.5, "y": 2.25, "width": 7.5, "height": 4.75},
        "type": "Plot",
        "minW": 4.0,
        "minH": 3.0,
        "visible": True,
        "locked": True,
        "moved": True,
        "static": False,
        "color": "#FFAA00",
        "plot_tile": {
            "plot_type": "area",
            "x_axis": "timestamp",
            "y_axis": "cumulative_value",
            "plot_scale_x": "time",
            "plot_scale_y": "linear",
        },
    }

    import_request = {
        "project_name": TEST_PROJECT,
        "template": template,
        "tab_id": tab_id,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tile/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    # Verify positioning and properties
    created_tile_id = data["created_ids"]["tile_id"]
    get_response = await _get_tile(client, tile_id=created_tile_id)
    tile_data = get_response.json()

    assert tile_data["position"]["x"] == 3.5
    assert tile_data["position"]["y"] == 2.25
    assert tile_data["position"]["width"] == 7.5
    assert tile_data["position"]["height"] == 4.75
    assert tile_data["minW"] == 4.0
    assert tile_data["minH"] == 3.0
    assert tile_data["locked"] is True
    assert tile_data["moved"] is True
    assert tile_data["static"] is False
    assert tile_data["color"] == "#FFAA00"


@pytest.mark.anyio
async def test_export_import_tile_template_with_valid_schema_roundtrip(
    client: AsyncClient,
):
    """Test exporting and then importing a tile template with valid schema (roundtrip)"""
    # Create interface and tab
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create complex editor tile
    tile_response = await _create_test_editor_tile(
        client,
        tab_id,
        name="roundtrip_editor",
        file_type="python",
        file_name="complex_analysis.py",
        content="""
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# Complex data analysis
def analyze_data(filepath):
    df = pd.read_csv(filepath)

    # Statistical analysis
    stats = df.describe()
    correlations = df.corr()

    # Visualization
    plt.figure(figsize=(12, 8))
    plt.subplot(2, 2, 1)
    df.hist(bins=50)

    return stats, correlations

if __name__ == "__main__":
    results = analyze_data("data.csv")
    print(results)
        """,
        width=10,
        height=8,
        x=1,
        y=1,
        color="#00FFAA",
    )
    tile_id = tile_response.json()["id"]

    # Export the tile template
    export_request = {
        "tile_id": tile_id,
        "include_metadata": True,
        "description": "Complex Python editor roundtrip test",
        "tags": ["roundtrip", "python", "editor"],
    }

    export_response = await client.post(
        "/v0/tile/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert export_response.status_code == 200
    exported_template = export_response.json()["template"]

    # Delete the original tile
    await _delete_tile(client, tile_id=tile_id)

    # Import the template back to the same tab
    import_request = {
        "project_name": TEST_PROJECT,
        "template": exported_template,
        "tab_id": tab_id,
        "validate_first": False,
        "auto_sanitize": False,
    }

    import_response = await client.post(
        "/v0/tile/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert import_response.status_code == 200
    import_data = import_response.json()

    assert import_data["success"] is True
    assert import_data["import_stats"]["tiles"] == 1

    # Verify the imported tile matches the original
    created_tile_id = import_data["created_ids"]["tile_id"]
    get_response = await _get_tile(client, tile_id=created_tile_id)
    imported_tile = get_response.json()

    assert imported_tile["name"] == "roundtrip_editor"
    assert imported_tile["type"] == "Editor"
    assert imported_tile["position"]["x"] == 1
    assert imported_tile["position"]["y"] == 1
    assert imported_tile["position"]["width"] == 10
    assert imported_tile["position"]["height"] == 8
    assert imported_tile["color"] == "#00FFAA"

    # Verify editor content was preserved
    editor_data = imported_tile["editor_tile"]
    assert editor_data["file_type"] == "python"
    assert editor_data["file_name"] == "complex_analysis.py"
    assert "import pandas as pd" in editor_data["content"]
    assert "analyze_data" in editor_data["content"]
    assert "matplotlib.pyplot" in editor_data["content"]


@pytest.mark.anyio
async def test_import_tile_template_with_valid_schema_minimal_template(
    client: AsyncClient,
):
    """Test importing tile template with valid schema containing minimal required fields"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Minimal template with only required fields
    minimal_template = {
        "name": "minimal_tile",
        "position": {"x": 0, "y": 0, "width": 4, "height": 3},
        "type": "View",
        "view_tile": {
            "base_index": "text",
        },
    }

    import_request = {
        "project_name": TEST_PROJECT,
        "template": minimal_template,
        "tab_id": tab_id,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/tile/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["tiles"] == 1

    # Verify minimal tile was created with defaults
    created_tile_id = data["created_ids"]["tile_id"]
    get_response = await _get_tile(client, tile_id=created_tile_id)
    tile_data = get_response.json()

    assert tile_data["name"] == "minimal_tile"
    assert tile_data["type"] == "View"
    assert tile_data["visible"] is True  # Default value
    assert tile_data["locked"] is False  # Default value
    assert tile_data["view_tile"]["base_index"] == "text"


@pytest.mark.anyio
async def test_export_tile_template_with_valid_schema_no_specialized_data(
    client: AsyncClient,
):
    """Test exporting tile template with valid schema for tile without specialized data"""
    interface_response = await _create_test_interface(client)
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(client, interface_id)
    tab_id = tab_response.json()["id"]

    # Create tile without type (no specialized data)
    tile_response = await _create_test_tile(
        client,
        tab_id,
        name="generic_tile",
        width=4,
        height=3,
        x=0,
        y=0,
        visible=True,
        color="#CCCCCC",
    )
    tile_id = tile_response.json()["id"]

    export_request = {
        "tile_id": tile_id,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/tile/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    template = response.json()["template"]

    assert template["name"] == "generic_tile"
    assert template["type"] is None
    assert template["color"] == "#CCCCCC"

    # Should not have any specialized tile data fields
    assert "table_tile" not in template or template["table_tile"] is None
    assert "plot_tile" not in template or template["plot_tile"] is None
    assert "view_tile" not in template or template["view_tile"] is None
    assert "editor_tile" not in template or template["editor_tile"] is None
    assert "terminal_tile" not in template or template["terminal_tile"] is None


@pytest.mark.anyio
async def test_tile_context_validation(client: AsyncClient):
    """Test that tile API validates context references"""
    project_name = f"test-tile-context-{uuid.uuid4()}"
    context_name = "valid-context"
    invalid_context = "nonexistent-context"

    # Create project and context
    await _create_project(client, project_name)
    await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "Valid context"},
        headers=HEADERS,
    )

    # Create interface and tab
    interface_response = await _create_test_interface(
        client,
        name="test-interface",
        project=project_name,
    )
    interface_id = interface_response.json()["id"]

    tab_response = await _create_test_tab(
        client,
        name="test-tab",
        interface_id=interface_id,
    )
    tab_id = tab_response.json()["id"]

    # Create tile with valid context - should succeed
    response = await client.post(
        "/v0/tile/",
        json={
            "name": "valid-context-tile",
            "tab_id": tab_id,
            "type": "Table",
            "context": context_name,
            "position": {"x": 0, "y": 0, "width": 6, "height": 4},
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["context"] == context_name

    # Try to create tile with invalid context - should fail
    response = await client.post(
        "/v0/tile/",
        json={
            "name": "invalid-context-tile",
            "tab_id": tab_id,
            "type": "Table",
            "context": invalid_context,
            "position": {"x": 6, "y": 0, "width": 6, "height": 4},
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert f"Context '{invalid_context}' not found" in response.json()["detail"]

    # Clean up
    await _delete_project(client, project_name)
