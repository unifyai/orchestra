import json
import os

import pytest
from httpx import AsyncClient

from .test_interface import _create_test_interface, _list_interfaces, _update_interface
from .test_legacy_interface import _create_context, _create_interface, _create_project
from .test_log import _create_derived_entry, _create_log, _update_logs
from .test_tab import _create_test_tab, _list_tabs, _update_tab
from .test_tile import (
    _create_test_editor_tile,
    _create_test_plot_tile,
    _create_test_table_tile,
    _create_test_terminal_tile,
    _create_test_view_tile,
    _list_tiles,
    _update_tile,
)

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


@pytest.mark.anyio
async def test_create_project(client: AsyncClient):
    url = "/v0/project"
    project_data = {"name": "test-project"}
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Project created successfully!"


@pytest.mark.anyio
async def test_create_existing_project(client: AsyncClient):
    url = "/v0/project"
    project_data = {
        "name": "existing-project",
    }

    # Create the project first
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # Try to create the same project again
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 400
    assert (
        response.json()["detail"] == "A logging project with this name already exists."
    )


@pytest.mark.anyio
async def test_delete_project(client: AsyncClient):
    url = "/v0/project/test-project"

    # Create a project first to delete it
    create_response = await client.post(
        "/v0/project",
        json={"name": "test-project"},
        headers=HEADERS,
    )
    assert create_response.status_code == 200

    # Now delete the project
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project deleted successfully"


@pytest.mark.anyio
async def test_delete_nonexistent_project(client: AsyncClient):
    url = "/v0/project/nonexistent-project"
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Project nonexistent-project not found."


@pytest.mark.anyio
async def test_rename_project(client: AsyncClient):
    create_url = "/v0/project"
    rename_url = "/v0/project/test-project"
    project_data = {"name": "test-project"}

    # Create a project to rename
    respose = await client.post(create_url, json=project_data, headers=HEADERS)

    # Rename the project
    rename_data = {"name": "renamed-project"}
    response = await client.patch(rename_url, json=rename_data, headers=HEADERS)
    print(response)
    assert response.status_code == 200
    assert response.json()["info"] == "Project renamed successfully!"


@pytest.mark.anyio
async def test_rename_nonexistent_project(client: AsyncClient):
    url = "/v0/project/nonexistent-project"
    project_data = {"name": "renamed-project"}
    response = await client.patch(url, json=project_data, headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Project nonexistent-project not found."


@pytest.mark.anyio
async def test_list_projects(client: AsyncClient):
    # Add two projects first
    await client.post("/v0/project", json={"name": "project_a"}, headers=HEADERS)
    await client.post("/v0/project", json={"name": "project_b"}, headers=HEADERS)

    # List the projects
    url = "/v0/projects"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200
    projects = response.json()
    assert "project_a" in projects
    assert "project_b" in projects


@pytest.mark.anyio
async def test_delete_project_logs(client: AsyncClient):
    # Create a test project with context, interface and logs
    project = "test_project"
    context = "test_context"
    interface_name = "test_interface"
    items = [
        {
            "i": "n0",
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "tab": None,
            "moved": False,
            "static": False,
        },
    ]
    new_counter = 1

    # Create project and its components
    await _create_project(client, project)
    await _create_context(client, project, context, "test description")
    await _create_interface(
        client,
        interface_name,
        project,
        items,
        new_counter,
    )

    # Create some logs in the project
    for i in range(3):
        _ = await _create_log(
            client,
            project,
            context={"name": context},
            entries={"test_key": f"test_value_{i}", "another_key": 123},
        )

    # Call delete_project_logs endpoint
    delete_response = await client.delete(
        f"/v0/project/{project}/logs",
        headers=HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify logs are deleted
    logs_response = await client.get(
        f"/v0/logs",
        headers=HEADERS,
        params={"project": project},
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 0  # No logs should remain

    # Verify context still exists
    context_response = await client.get(
        f"/v0/project/{project}/contexts",
        headers=HEADERS,
    )
    assert context_response.status_code == 200
    contexts = context_response.json()
    assert len(contexts) > 0
    assert contexts[0]["name"] == context

    # Verify interface still exists
    interface_response = await client.get(
        f"/v0/interface?name={interface_name}&project={project}",
        headers=HEADERS,
    )
    assert interface_response.status_code == 200
    interfaces = interface_response.json()
    assert len(interfaces) > 0
    assert interfaces[0]["name"] == interface_name


@pytest.mark.anyio
async def test_delete_project_contexts(client: AsyncClient):
    # Create a test project with multiple contexts, interface and logs
    project = "test_project_contexts"
    context1 = "test_context_1"
    context2 = "test_context_2"
    interface_name = "test_interface"
    items = [
        {
            "i": "n0",
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "tab": None,
            "moved": False,
            "static": False,
        },
    ]
    new_counter = 1

    # Create project and its components
    await _create_project(client, project)
    await _create_context(client, project, context1, "test description 1")
    await _create_context(client, project, context2, "test description 2")
    await _create_interface(
        client,
        interface_name,
        project,
        items,
        new_counter,
    )

    # Create logs for each context
    await _create_log(
        client,
        project,
        context={"name": context1},
        entries={"test_key": "value1"},
    )
    await _create_log(
        client,
        project,
        context={"name": context2},
        entries={"test_key": "value2"},
    )

    # Call delete_project_contexts endpoint
    delete_response = await client.delete(
        f"/v0/project/{project}/contexts",
        headers=HEADERS,
    )
    assert delete_response.status_code == 200
    assert (
        delete_response.json()["info"]
        == "Project contexts and logs deleted successfully!"
    )

    # Verify logs are deleted
    logs_response = await client.get(
        f"/v0/logs",
        headers=HEADERS,
        params={"project": project},
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert len(logs) == 0  # No logs should remain

    # Verify contexts are deleted
    contexts_response = await client.get(
        f"/v0/project/{project}/contexts",
        headers=HEADERS,
    )
    assert contexts_response.status_code == 200
    contexts = contexts_response.json()
    assert len(contexts) == 0  # No contexts should remain

    # Verify interface still exists
    interface_response = await client.get(
        f"/v0/interface?name={interface_name}&project={project}",
        headers=HEADERS,
    )
    assert interface_response.status_code == 200
    interfaces = interface_response.json()
    assert len(interfaces) > 0
    assert interfaces[0]["name"] == interface_name


@pytest.mark.anyio
async def test_share_project(client: AsyncClient):
    """
    Test the admin endpoint for sharing a project between users.
    This test verifies that an admin can share a project from one user to another.
    It also verifies that changes to the project are reflected to the new user.
    """
    # Set up admin headers with admin API key
    admin_api_key = str(os.getenv("ORCHESTRA_ADMIN_KEY"))
    admin_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_api_key}",
    }

    # create a new user
    response = await client.post(
        "v0/admin/auth-user",
        json={
            "email": "test_recipient_user@example.com",
            "name": "test_recipient_user",
        },
        headers=admin_headers,
    )
    data = response.json()
    to_user_id = data["id"]
    from_user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

    # Create a test project first (owned by the current user)
    project_name = "shared_test_project"
    interface_name = "shared_test_interface"
    context_name = "shared_test_context"
    _ = await _create_project(client, project_name)

    # create a new context in the project
    _ = await _create_context(client, project_name, context_name, "test description")
    # create a new interface in the project
    items = [
        {
            "i": "n0",
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "tab": None,
            "moved": False,
            "static": False,
        },
    ]
    new_counter = 1
    _ = await _create_interface(
        client,
        interface_name,
        project_name,
        items,
        new_counter,
    )
    # create a new log in the project
    _ = await _create_log(
        client,
        project_name,
        context={"name": context_name},
        entries={"test_key": "value1"},
    )

    # Call the share project endpoint
    url = "v0/admin/share-project"
    share_data = {
        "from_user_id": from_user_id,
        "to_user_id": to_user_id,
        "project_name": project_name,
    }

    response = await client.post(url, json=share_data, headers=admin_headers)

    # Verify the response
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Project shared successfully!"

    # get the api key for the new user
    response = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={to_user_id}",
        headers=admin_headers,
    )
    data = response.json()
    to_user_api_key = data["apiKey"]
    new_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {to_user_api_key}",
    }

    # 1) Verify the new user can acces the project
    response = await client.get(
        f"/v0/projects",
        headers=new_headers,
    )
    assert response.status_code == 200, response.json()
    assert project_name in response.json()

    # 2) Verify the new user can access the project's contexts
    response = await client.get(
        f"/v0/project/{project_name}/contexts",
        headers=new_headers,
    )
    assert response.status_code == 200, response.json()
    contexts = response.json()
    assert len(contexts) > 0
    assert context_name in [context["name"] for context in contexts]

    # 3) Verify the new user can access the project's interfaces
    response = await client.get(
        f"/v0/interface",
        params={"project": project_name},
        headers=new_headers,
    )
    assert response.status_code == 200, response.json()
    interfaces = response.json()
    assert len(interfaces) > 0
    assert interface_name in [interface["name"] for interface in interfaces]

    # 4) Verify the new user can access the project's logs
    response = await client.get(
        f"/v0/logs",
        params={"project": project_name, "context": context_name},
        headers=new_headers,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert len(data["logs"]) > 0
    assert "test_key" in data["logs"][0]["entries"]

    # 5) Update the logs and verify the new user can access the updated logs
    response = await _update_logs(
        client,
        log_ids=[1],
        context={"name": context_name},
        entries={"new_key": "value2"},
    )
    assert response.status_code == 200, response.json()

    # get the logs from the new user
    response = await client.get(
        f"/v0/logs",
        params={"project": project_name, "context": context_name},
        headers=new_headers,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert len(data["logs"]) > 0
    entries = data["logs"][0]["entries"]
    assert "test_key" in entries
    assert "new_key" in entries


@pytest.mark.anyio
async def test_duplicate_project(client: AsyncClient):
    """
    Test the admin endpoint for duplicating a project with new interfaces.
    This test verifies that an admin can create a deep copy of a project from one user to another.
    This test focuses solely on new interfaces, tabs, and tiles (without legacy interfaces).
    It also verifies that derived logs are duplicated.
    It also verifies that changes to the original project do not affect the duplicate.
    """
    # Set up admin headers with admin API key
    admin_api_key = str(os.getenv("ORCHESTRA_ADMIN_KEY"))
    admin_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_api_key}",
    }

    # Create a new target user
    response = await client.post(
        "v0/admin/auth-user",
        json={
            "email": "test_new_duplicate_target_with_new_interfaces@example.com",
            "name": "test_new_duplicate_target_with_new_interfaces",
        },
        headers=admin_headers,
    )
    data = response.json()
    target_user_id = data["id"]
    source_user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

    # Create a source project with contexts
    source_project_name = "new_source_project_with_new_interfaces"
    target_project_name = "new_duplicated_project_with_new_interfaces"
    context_name = "new_test_context_with_new_interfaces"

    # Create project and its components
    await _create_project(client, source_project_name)
    await _create_context(client, source_project_name, context_name, "test description")

    # Create a log in the source project (to test later that updates don't affect duplicate)
    log_id = await _create_log(
        client,
        source_project_name,
        context={"name": context_name},
        entries={"source_key": "source_value"},
    )

    # Create derived logs to test they are also duplicated
    key = "derived_key"
    equation = "{Table:source_key} + ' hello'"
    referenced_logs = {"Table": {"filter_expr": "", "context": context_name}}
    response = await _create_derived_entry(
        client,
        source_project_name,
        context=context_name,
        key=key,
        equation=equation,
        referenced_logs=referenced_logs,
    )
    assert response.status_code == 200, response.json()

    key = "derived_key2"
    equation = "{Table:source_key} + ' world'"
    referenced_logs = {"Table": [1]}
    response = await _create_derived_entry(
        client,
        source_project_name,
        context=context_name,
        key=key,
        equation=equation,
        referenced_logs=referenced_logs,
    )
    assert response.status_code == 200, response.json()

    # Create new interface with tabs and tiles
    new_interface_name = "new_source_interface"
    interface_response = await _create_test_interface(
        client,
        name=new_interface_name,
        project=source_project_name,
    )
    assert interface_response.status_code == 201, interface_response.json()
    new_interface_data = interface_response.json()
    new_interface_id = new_interface_data["id"]

    # Create tabs in the new interface
    tab1_name = "New Tab 1"
    tab1_response = await _create_test_tab(
        client,
        new_interface_id,
        name=tab1_name,
        active=True,
        order=0,
    )
    assert tab1_response.status_code == 201, tab1_response.json()
    tab1_data = tab1_response.json()
    tab1_id = tab1_data["id"]

    tab2_name = "New Tab 2"
    tab2_response = await _create_test_tab(
        client,
        new_interface_id,
        name=tab2_name,
        active=False,
        order=1,
    )
    assert tab2_response.status_code == 201, tab2_response.json()
    tab2_data = tab2_response.json()
    tab2_id = tab2_data["id"]

    # Create different types of tiles in the tabs
    # 1. Table Tile
    table_tile_name = "New Table"
    table_response = await _create_test_table_tile(
        client,
        tab1_id,
        name=table_tile_name,
        table_type="fixed",
        column_context=json.dumps(["Column 1", "Column 2"]),
        width=4,
        height=3,
        x=0,
        y=0,
    )
    assert table_response.status_code == 201, table_response.json()

    # 2. Plot Tile
    plot_tile_name = "New Plot"
    plot_response = await _create_test_plot_tile(
        client,
        tab1_id,
        name=plot_tile_name,
        plot_type="scatter",
        x_axis="x",
        y_axis="y",
        width=4,
        height=3,
        x=4,
        y=0,
    )
    assert plot_response.status_code == 201, plot_response.json()

    # 3. View Tile
    view_tile_name = "New View"
    view_response = await _create_test_view_tile(
        client,
        tab2_id,
        name=view_tile_name,
        base_index="markdown",
        width=4,
        height=3,
        x=0,
        y=0,
    )
    assert view_response.status_code == 201, view_response.json()

    # 4. Editor Tile
    editor_tile_name = "New Editor"
    editor_response = await _create_test_editor_tile(
        client,
        tab2_id,
        name=editor_tile_name,
        file_type="python",
        content="print('New Hello, World!')",
        width=4,
        height=3,
        x=4,
        y=0,
    )
    assert editor_response.status_code == 201, editor_response.json()

    # 5. Terminal Tile
    terminal_tile_name = "New Terminal"
    terminal_response = await _create_test_terminal_tile(
        client,
        tab2_id,
        name=terminal_tile_name,
        shell_type="bash",
        width=4,
        height=3,
        x=0,
        y=3,
    )
    assert terminal_response.status_code == 201, terminal_response.json()

    # Call the duplicate project endpoint
    url = "v0/admin/duplicate-project"
    duplicate_data = {
        "from_user_id": source_user_id,
        "from_project_name": source_project_name,
        "to_user_id": target_user_id,
        "new_project_name": target_project_name,
    }

    response = await client.post(url, json=duplicate_data, headers=admin_headers)

    # Verify the response
    assert response.status_code == 200, response.json()
    assert (
        f"Project '{source_project_name}' duplicated successfully"
        in response.json()["info"]
    )

    # Verify the counts of duplicated resources
    result = response.json()["details"]
    assert result["contexts_copied"] >= 1
    assert result["field_types_copied"] >= 1
    assert result["logs_copied"] >= 1
    assert result["derived_logs_copied"] >= 1
    # Verify interfaces are copied
    assert result["interfaces_copied"] >= 1
    # Verify tabs and specialized tiles are copied
    assert result["tabs_copied"] >= 2  # We created 2 tabs
    assert (
        result["tiles_copied"] >= 5
    )  # We created 5 tiles (table, plot, view, editor, terminal)
    assert result["table_tiles_copied"] >= 1
    assert result["plot_tiles_copied"] >= 1
    assert result["view_tiles_copied"] >= 1
    assert result["editor_tiles_copied"] >= 1
    assert result["terminal_tiles_copied"] >= 1

    # Get the API key for the target user
    response = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={target_user_id}",
        headers=admin_headers,
    )
    data = response.json()
    target_user_api_key = data["apiKey"]
    target_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {target_user_api_key}",
    }

    # 1) Verify the target user can access the duplicated project
    response = await client.get(
        f"/v0/projects",
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    assert target_project_name in response.json()

    # 2) Verify the target user can access the project's contexts
    response = await client.get(
        f"/v0/project/{target_project_name}/contexts",
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    contexts = response.json()
    assert len(contexts) > 0
    assert context_name in [context["name"] for context in contexts]

    # 3) Verify the target user can access the new interfaces
    response = await client.get(
        f"/v0/interfaces/list",
        params={"project": target_project_name},
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    new_interfaces = response.json()
    assert len(new_interfaces) > 0

    # Find the duplicated new interface
    duplicated_interface = None
    for interface in new_interfaces:
        if interface["name"] == new_interface_name:
            duplicated_interface = interface
            break

    assert (
        duplicated_interface is not None
    ), f"New interface '{new_interface_name}' not found in duplicated project"
    duplicated_interface_id = duplicated_interface["id"]

    # 4) Verify the target user can access the tabs in the new interface
    response = await client.get(
        f"/v0/tab/list",
        params={"interface_id": duplicated_interface_id},
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    tabs = response.json()
    assert len(tabs) >= 2, "Not all tabs were duplicated"

    # Find the duplicated tabs
    tab_names = [tab["name"] for tab in tabs]
    assert (
        tab1_name in tab_names
    ), f"Tab '{tab1_name}' not found in duplicated interface"
    assert (
        tab2_name in tab_names
    ), f"Tab '{tab2_name}' not found in duplicated interface"

    # Get the duplicated tab IDs
    duplicated_tab1_id = None
    duplicated_tab2_id = None
    for tab in tabs:
        if tab["name"] == tab1_name:
            duplicated_tab1_id = tab["id"]
        elif tab["name"] == tab2_name:
            duplicated_tab2_id = tab["id"]

    # 5) Verify the target user can access the tiles in tab 1
    response = await client.get(
        f"/v0/tile/list",
        params={"tab_id": duplicated_tab1_id},
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    tab1_tiles = response.json()
    assert len(tab1_tiles) >= 2, "Not all tiles in tab 1 were duplicated"

    # Verify the table and plot tiles exist in tab 1
    tile_names = [tile["name"] for tile in tab1_tiles]
    assert (
        table_tile_name in tile_names
    ), f"Table tile '{table_tile_name}' not found in tab 1"
    assert (
        plot_tile_name in tile_names
    ), f"Plot tile '{plot_tile_name}' not found in tab 1"

    # Get duplicated tile details to verify specialized properties
    # Find the table tile
    duplicated_table_tile = None
    for tile in tab1_tiles:
        if tile["name"] == table_tile_name:
            duplicated_table_tile = tile
            break

    assert duplicated_table_tile is not None, "Table tile not found"
    assert duplicated_table_tile["type"] == "Table", "Tile type not preserved"
    assert (
        "table_tile" in duplicated_table_tile
    ), "Table tile specialized data not found"
    assert (
        duplicated_table_tile["table_tile"]["table_type"] == "fixed"
    ), "Table type not preserved"

    # 6) Verify the target user can access the tiles in tab 2
    response = await client.get(
        f"/v0/tile/list",
        params={"tab_id": duplicated_tab2_id},
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    tab2_tiles = response.json()
    assert len(tab2_tiles) >= 3, "Not all tiles in tab 2 were duplicated"

    # Verify the view, editor, and terminal tiles exist in tab 2
    tile_names = [tile["name"] for tile in tab2_tiles]
    assert (
        view_tile_name in tile_names
    ), f"View tile '{view_tile_name}' not found in tab 2"
    assert (
        editor_tile_name in tile_names
    ), f"Editor tile '{editor_tile_name}' not found in tab 2"
    assert (
        terminal_tile_name in tile_names
    ), f"Terminal tile '{terminal_tile_name}' not found in tab 2"

    # Find the editor tile
    duplicated_editor_tile = None
    for tile in tab2_tiles:
        if tile["name"] == editor_tile_name:
            duplicated_editor_tile = tile
            break

    assert duplicated_editor_tile is not None, "Editor tile not found"
    assert duplicated_editor_tile["type"] == "Editor", "Tile type not preserved"
    assert (
        "editor_tile" in duplicated_editor_tile
    ), "Editor tile specialized data not found"
    assert (
        duplicated_editor_tile["editor_tile"]["content"] == "print('New Hello, World!')"
    ), "Editor content not preserved"
    assert (
        duplicated_editor_tile["editor_tile"]["file_type"] == "python"
    ), "Editor file type not preserved"

    # Find the terminal tile
    duplicated_terminal_tile = None
    for tile in tab2_tiles:
        if tile["name"] == terminal_tile_name:
            duplicated_terminal_tile = tile
            break

    assert duplicated_terminal_tile is not None, "Terminal tile not found"
    assert (
        duplicated_terminal_tile["type"] == "Terminal"
    ), "Terminal tile type not preserved"
    assert (
        "terminal_tile" in duplicated_terminal_tile
    ), "Terminal tile specialized data not found"
    assert (
        duplicated_terminal_tile["terminal_tile"]["shell_type"] == "bash"
    ), "Terminal shell type not preserved"

    # 7) Verify the target user can access the logs
    response = await client.get(
        f"/v0/logs",
        params={"project": target_project_name, "context": context_name},
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert len(data["logs"]) > 0
    assert "source_key" in data["logs"][0]["entries"]
    assert data["logs"][0]["entries"]["source_key"] == "source_value"

    # Verify derived logs were also duplicated
    assert "derived_key" in data["logs"][0]["derived_entries"]
    assert "derived_key2" in data["logs"][0]["derived_entries"]
    assert data["logs"][0]["derived_entries"]["derived_key"] == "source_value hello"
    assert data["logs"][0]["derived_entries"]["derived_key2"] == "source_value world"

    # 8) Update the source project logs and verify the changes don't affect the duplicate
    response = await _update_logs(
        client,
        log_ids=[1],
        context={"name": context_name},
        entries={"updated_key": "updated_value"},
    )
    assert response.status_code == 200, response.json()

    # Verify the source project has the updated log
    response = await client.get(
        f"/v0/logs",
        params={"project": source_project_name, "context": context_name},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    source_logs = response.json()["logs"]
    assert "updated_key" in source_logs[0]["entries"]

    # Verify the duplicated project does NOT have the updated log
    response = await client.get(
        f"/v0/logs",
        params={"project": target_project_name, "context": context_name},
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    target_logs = response.json()["logs"]
    assert "updated_key" not in target_logs[0]["entries"]

    # 9) Test that changes to source project interfaces/tabs/tiles don't affect duplicate
    # Update the source interface name using helper function
    source_interface_update_response = await _update_interface(
        client,
        interface_id=new_interface_id,
        update_data={"name": "Updated Source Interface Name"},
    )
    assert source_interface_update_response.status_code == 200

    # Update a tab in the source project using helper function
    source_tab_update_response = await _update_tab(
        client,
        tab_id=tab1_id,
        update_data={"name": "Updated Source Tab Name"},
    )
    assert source_tab_update_response.status_code == 200

    # Update a tile in the source project using helper function
    source_tiles_response = await _list_tiles(client, tab_id=tab1_id)
    assert source_tiles_response.status_code == 200
    source_tiles = source_tiles_response.json()
    source_table_tile = None
    for tile in source_tiles:
        if tile["name"] == table_tile_name:
            source_table_tile = tile
            break

    if source_table_tile:
        source_tile_update_response = await _update_tile(
            client,
            tile_id=source_table_tile["id"],
            update_data={"name": "Updated Source Table Tile"},
        )
        assert source_tile_update_response.status_code == 200

    # Verify the duplicated interface name hasn't changed using helper function
    target_interfaces_response = await _list_interfaces(
        client,
        project=target_project_name,
    )
    target_interfaces_response.headers = target_headers  # Use target user headers
    target_interfaces_response = await client.get(
        f"/v0/interfaces/list?project={target_project_name}",
        headers=target_headers,
    )
    assert (
        target_interfaces_response.status_code == 200
    ), target_interfaces_response.json()
    target_interfaces = target_interfaces_response.json()
    target_interface_names = [interface["name"] for interface in target_interfaces]
    assert (
        new_interface_name in target_interface_names
    )  # Original name should still be there
    assert "Updated Source Interface Name" not in target_interface_names

    # Verify the duplicated tab name hasn't changed using helper function
    target_tabs_response = await _list_tabs(
        client,
        interface_id=duplicated_interface_id,
    )
    target_tabs_response.headers = target_headers  # Use target user headers
    target_tabs_response = await client.get(
        f"/v0/tab/list?interface_id={duplicated_interface_id}",
        headers=target_headers,
    )
    assert target_tabs_response.status_code == 200, target_tabs_response.json()
    target_tabs = target_tabs_response.json()
    target_tab_names = [tab["name"] for tab in target_tabs]
    assert tab1_name in target_tab_names  # Original name should still be there
    assert "Updated Source Tab Name" not in target_tab_names

    # Verify the duplicated tile name hasn't changed using helper function
    target_tiles_response = await _list_tiles(client, tab_id=duplicated_tab1_id)
    target_tiles_response.headers = target_headers  # Use target user headers
    target_tiles_response = await client.get(
        f"/v0/tile/list?tab_id={duplicated_tab1_id}",
        headers=target_headers,
    )
    assert target_tiles_response.status_code == 200, target_tiles_response.json()
    target_tiles = target_tiles_response.json()
    target_tile_names = [tile["name"] for tile in target_tiles]
    assert table_tile_name in target_tile_names  # Original name should still be there
    assert "Updated Source Table Tile" not in target_tile_names


if __name__ == "__main__":
    pass
