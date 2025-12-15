import json
import os

import pytest
from httpx import AsyncClient

from .test_interface import _create_test_interface, _list_interfaces, _update_interface
from .test_legacy_interface import _create_context, _create_project
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
async def test_create_project_with_description(client: AsyncClient):
    url = "/v0/project"
    project_data = {
        "name": "test-project-with-desc",
        "description": "This is a test project with a description",
    }
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Project created successfully!"


@pytest.mark.anyio
async def test_create_project_description_too_long(client: AsyncClient):
    url = "/v0/project"
    # Create a description longer than 256 characters
    long_description = "a" * 257
    project_data = {
        "name": "test-project-long-desc",
        "description": long_description,
    }
    response = await client.post(url, json=project_data, headers=HEADERS)
    assert response.status_code == 422, response.json()


@pytest.mark.anyio
async def test_create_existing_project(client: AsyncClient):
    url = "/v0/project"
    project_data = {
        "name": "existing-project",
        "description": "Original description",
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
        json={"name": "test-project", "description": "Project to be deleted"},
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
    assert "nonexistent-project" in response.json()["detail"]
    assert "not found" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_project_name(client: AsyncClient):
    create_url = "/v0/project"
    update_url = "/v0/project/test-project"
    project_data = {"name": "test-project", "description": "Original description"}

    # Create a project to rename
    respose = await client.post(create_url, json=project_data, headers=HEADERS)

    # Rename the project
    rename_data = {"name": "renamed-project"}
    response = await client.patch(update_url, json=rename_data, headers=HEADERS)
    print(response)
    assert response.status_code == 200
    assert response.json()["info"] == "Project updated successfully!"


@pytest.mark.anyio
async def test_update_project_description(client: AsyncClient):
    create_url = "/v0/project"
    update_url = "/v0/project/test-project-desc"
    project_data = {"name": "test-project-desc", "description": "Original description"}

    # Create a project to update
    response = await client.post(create_url, json=project_data, headers=HEADERS)
    assert response.status_code == 200

    # Update the project description
    update_data = {"description": "Updated description"}
    response = await client.patch(update_url, json=update_data, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project updated successfully!"


@pytest.mark.anyio
async def test_update_project_description_too_long(client: AsyncClient):
    create_url = "/v0/project"
    update_url = "/v0/project/test-project-desc-long"
    project_data = {
        "name": "test-project-desc-long",
        "description": "Original description",
    }

    # Create a project to update
    response = await client.post(create_url, json=project_data, headers=HEADERS)
    assert response.status_code == 200

    # Try to update with description that's too long
    long_description = "a" * 257
    update_data = {"description": long_description}
    response = await client.patch(update_url, json=update_data, headers=HEADERS)
    assert response.status_code == 422, response.json()


@pytest.mark.anyio
async def test_update_project_name_and_description(client: AsyncClient):
    create_url = "/v0/project"
    update_url = "/v0/project/test-project-both"
    project_data = {"name": "test-project-both", "description": "Original description"}

    # Create a project to update
    response = await client.post(create_url, json=project_data, headers=HEADERS)
    assert response.status_code == 200

    # Update both name and description
    update_data = {"name": "renamed-project-both", "description": "Updated description"}
    response = await client.patch(update_url, json=update_data, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project updated successfully!"


@pytest.mark.anyio
async def test_update_nonexistent_project(client: AsyncClient):
    url = "/v0/project/nonexistent-project"
    project_data = {"name": "updated-project", "description": "New description"}
    response = await client.patch(url, json=project_data, headers=HEADERS)
    assert response.status_code == 404
    assert "nonexistent-project" in response.json()["detail"]
    assert "not found" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_project_icon(client: AsyncClient):
    create_url = "/v0/project"
    update_url = "/v0/project/icon-project"
    project_data = {"name": "icon-project"}

    # Create new project
    response = await client.post(create_url, json=project_data, headers=HEADERS)
    assert response.status_code == 200

    # Update icon
    update_data = {"icon": "rocket"}
    resp = await client.patch(update_url, json=update_data, headers=HEADERS)
    assert resp.status_code == 200

    # Fetch details and confirm icon
    detail_resp = await client.get(update_url, headers=HEADERS)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["icon"] == "rocket"


@pytest.mark.anyio
async def test_get_project_details(client: AsyncClient):
    # Create a project with description
    create_url = "/v0/project"
    project_data = {
        "name": "detailed-project",
        "description": "This project has detailed information",
    }
    create_response = await client.post(create_url, json=project_data, headers=HEADERS)
    assert create_response.status_code == 200

    # Get project details
    detail_url = "/v0/project/detailed-project"
    response = await client.get(detail_url, headers=HEADERS)
    assert response.status_code == 200

    project_details = response.json()
    assert project_details["name"] == "detailed-project"
    assert project_details["description"] == "This project has detailed information"
    assert "created_at" in project_details
    assert "updated_at" in project_details
    assert "is_versioned" in project_details


@pytest.mark.anyio
async def test_list_projects(client: AsyncClient):
    # Add two projects first - one with description, one without
    await client.post(
        "/v0/project",
        json={"name": "project_a", "description": "Project A description"},
        headers=HEADERS,
    )
    await client.post("/v0/project", json={"name": "project_b"}, headers=HEADERS)

    # List the projects - should still return simple list format
    url = "/v0/projects"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200
    projects = response.json()
    assert "project_a" in projects
    assert "project_b" in projects
    # Verify it's still a simple list, not detailed objects
    assert isinstance(projects, list)
    assert all(isinstance(project, str) for project in projects)


@pytest.mark.anyio
async def test_delete_project_logs(client: AsyncClient):
    # Create a test project with context, interface and logs
    project = "test_project"
    context = "test_context"
    interface_name = "test_interface"

    # Create project and its components
    await _create_project(client, project)
    await _create_context(client, project, context, "test description")
    await _create_test_interface(
        client,
        name=interface_name,
        project=project,
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
        f"/v0/interfaces/",
        params={"project": project, "name": interface_name},
        headers=HEADERS,
    )
    assert interface_response.status_code == 200
    interface = interface_response.json()
    assert interface["name"] == interface_name


@pytest.mark.anyio
async def test_delete_project_contexts(client: AsyncClient):
    # Create a test project with multiple contexts, interface and logs
    project = "test_project_contexts"
    context1 = "test_context_1"
    context2 = "test_context_2"
    interface_name = "test_interface"

    # Create project and its components
    await _create_project(client, project)
    await _create_context(client, project, context1, "test description 1")
    await _create_context(client, project, context2, "test description 2")
    await _create_test_interface(
        client,
        name=interface_name,
        project=project,
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
        f"/v0/interfaces/",
        params={"project": project, "name": interface_name},
        headers=HEADERS,
    )
    assert interface_response.status_code == 200
    interface = interface_response.json()
    assert interface["name"] == interface_name


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
    # Create project with description to test sharing preserves it
    create_response = await client.post(
        "/v0/project",
        json={"name": project_name, "description": "Shared project description"},
        headers=HEADERS,
    )
    assert create_response.status_code == 200

    # create a new context in the project
    _ = await _create_context(client, project_name, context_name, "test description")
    # create a new interface in the project
    _ = await _create_test_interface(
        client,
        name=interface_name,
        project=project_name,
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

    # get the org api key for the new user (project is now org-owned)
    # Use by-email endpoint which returns organizations list with API keys
    response = await client.get(
        "/v0/admin/auth-user/by-email?email=test_recipient_user@example.com",
        headers=admin_headers,
    )
    data = response.json()
    # After sharing, project belongs to an org, so we need the org API key
    assert "organizations" in data, "User should have organization memberships"
    assert len(data["organizations"]) > 0, "User should be member of at least one org"
    org_api_key = data["organizations"][0]["apiKey"]
    assert org_api_key is not None, "User should have org API key"
    new_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # 1) Verify the new user can access the project via org API key
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
        f"/v0/interfaces/list",
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

    # Create a source project with contexts and description
    source_project_name = "new_source_project_with_new_interfaces"
    target_project_name = "new_duplicated_project_with_new_interfaces"
    context_name = "new_test_context_with_new_interfaces"

    # Create project with description to test duplication preserves it
    create_response = await client.post(
        "/v0/project",
        json={
            "name": source_project_name,
            "description": "Source project with interfaces for duplication testing",
        },
        headers=HEADERS,
    )
    assert create_response.status_code == 200
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

    # 1b) Verify the duplicated project has the same description
    detail_response = await client.get(
        f"/v0/project/{target_project_name}",
        headers=target_headers,
    )
    assert detail_response.status_code == 200, detail_response.json()
    project_details = detail_response.json()
    assert project_details["name"] == target_project_name
    assert (
        project_details["description"]
        == "Source project with interfaces for duplication testing"
    )

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


@pytest.mark.anyio
async def test_export_project_template_with_valid_schema(client: AsyncClient):
    """Test exporting a project template with valid schema - all interfaces"""
    # Create a test project with multiple interfaces, tabs, and tiles
    project_name = "template_export_project"
    await _create_project(client, project_name)
    await _create_context(client, project_name, "test_context", "test description")

    # Create first interface with tabs and tiles
    interface1_name = "interface_1"
    interface1_response = await _create_test_interface(
        client,
        name=interface1_name,
        project=project_name,
    )
    interface1_id = interface1_response.json()["id"]

    # Create tabs and tiles for interface1
    tab1_response = await _create_test_tab(client, interface1_id, name="tab_1")
    tab1_id = tab1_response.json()["id"]

    await _create_test_table_tile(client, tab1_id, name="table_tile_1")
    await _create_test_plot_tile(client, tab1_id, name="plot_tile_1")

    # Create second interface
    interface2_name = "interface_2"
    interface2_response = await _create_test_interface(
        client,
        name=interface2_name,
        project=project_name,
    )
    interface2_id = interface2_response.json()["id"]

    tab2_response = await _create_test_tab(client, interface2_id, name="tab_2")
    tab2_id = tab2_response.json()["id"]

    await _create_test_view_tile(client, tab2_id, name="view_tile_1")

    # Export project template
    export_request = {
        "project": project_name,
        "include_metadata": True,
        "description": "Test project template",
        "tags": ["test", "export"],
        "template_name": "Test Template",
    }

    response = await client.post(
        "/v0/project/export_template",
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
    assert template["template_version"] == "1.0"
    assert template["description"] == "Test project template"
    assert "test" in template["tags"]
    assert "export" in template["tags"]
    assert len(template["interfaces"]) == 2

    # Verify interfaces are exported
    interface_names = [iface["name"] for iface in template["interfaces"]]
    assert interface1_name in interface_names
    assert interface2_name in interface_names

    # Verify export stats
    stats = data["export_stats"]
    assert stats["interfaces"] == 2
    assert stats["tabs"] >= 2
    assert stats["tiles"] >= 3


@pytest.mark.anyio
async def test_export_project_template_with_valid_schema_specific_interfaces(
    client: AsyncClient,
):
    """Test exporting a project template with valid schema - specific interfaces only"""
    project_name = "template_export_specific_project"
    await _create_project(client, project_name)

    # Create multiple interfaces
    interface1_response = await _create_test_interface(
        client,
        name="interface_to_export",
        project=project_name,
    )
    interface2_response = await _create_test_interface(
        client,
        name="interface_to_skip",
        project=project_name,
    )

    # Add content to both interfaces
    for interface_response in [interface1_response, interface2_response]:
        interface_id = interface_response.json()["id"]
        tab_response = await _create_test_tab(client, interface_id, name="test_tab")
        tab_id = tab_response.json()["id"]
        await _create_test_table_tile(client, tab_id, name="test_tile")

    # Export only specific interface
    export_request = {
        "project": project_name,
        "interface_names": ["interface_to_export"],
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/project/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    template = data["template"]
    assert len(template["interfaces"]) == 1
    assert template["interfaces"][0]["name"] == "interface_to_export"


@pytest.mark.anyio
async def test_export_project_template_with_valid_schema_checkpoints(
    client: AsyncClient,
):
    """Test exporting project template with valid schema from checkpoints"""
    project_name = "template_export_checkpoint_project"
    await _create_project(client, project_name)

    # Create interface and create checkpoint
    interface_response = await _create_test_interface(
        client,
        name="checkpoint_interface",
        project=project_name,
    )
    interface_id = interface_response.json()["id"]

    # Create checkpoint
    checkpoint_response = await client.post(
        f"/v0/interfaces/checkpoint?interface_id={interface_id}",
        headers=HEADERS,
    )
    assert checkpoint_response.status_code == 200

    # Update original interface after checkpoint
    await client.put(
        f"/v0/interfaces/?interface_id={interface_id}",
        json={"color": "#FF0000"},
        headers=HEADERS,
    )

    # Export from checkpoints
    export_request = {
        "project": project_name,
        "checkpoint": True,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/project/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    # Should export checkpoint version, not the updated version


@pytest.mark.anyio
async def test_import_project_template_with_valid_schema(client: AsyncClient):
    """Test importing a project template with valid schema"""
    # Create target project
    target_project = "template_import_target"
    await _create_project(client, target_project)
    await _create_context(client, target_project, "test_context", "test description")

    # Create a valid template
    template = {
        "interfaces": [
            {
                "name": "imported_interface",
                "color": "#00FF00",
                "tabs": [
                    {
                        "name": "imported_tab",
                        "visible": True,
                        "active": True,
                        "order": 0,
                        "color": "#0000FF",
                        "tiles": [
                            {
                                "name": "imported_tile",
                                "position": {"x": 0, "y": 0, "width": 4, "height": 3},
                                "type": "Table",
                                "visible": True,
                                "table_tile": {
                                    "table_type": "basic",
                                    "page_number": "1",
                                },
                            },
                        ],
                    },
                ],
            },
        ],
        "template_version": "1.0",
        "description": "Test import template",
        "tags": ["imported", "test"],
    }

    import_request = {
        "project": target_project,
        "template": template,
        "validate_first": False,  # Skip validation for v0
        "auto_sanitize": False,
        "overwrite_existing": False,
    }

    response = await client.post(
        "/v0/project/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert "import_stats" in data
    assert data["import_stats"]["interfaces"] == 1
    assert data["import_stats"]["tabs"] == 1
    assert data["import_stats"]["tiles"] == 1

    # Verify the interface was created
    interfaces_response = await client.get(
        f"/v0/interfaces/list?project={target_project}",
        headers=HEADERS,
    )
    assert interfaces_response.status_code == 200
    interfaces = interfaces_response.json()

    imported_interface = next(
        (iface for iface in interfaces if iface["name"] == "imported_interface"),
        None,
    )
    assert imported_interface is not None
    assert imported_interface["color"] == "#00FF00"


@pytest.mark.anyio
async def test_import_project_template_with_valid_schema_name_prefix(
    client: AsyncClient,
):
    """Test importing project template with valid schema and interface name prefix"""
    target_project = "template_import_prefix_target"
    await _create_project(client, target_project)

    template = {
        "interfaces": [
            {
                "name": "base_interface",
                "tabs": [
                    {
                        "name": "base_tab",
                        "tiles": [],
                    },
                ],
            },
        ],
        "template_version": "1.0",
    }

    import_request = {
        "project": target_project,
        "template": template,
        "interface_name_prefix": "imported_",
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/project/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200

    # Verify interface was created with prefix
    interfaces_response = await client.get(
        f"/v0/interfaces/list?project={target_project}",
        headers=HEADERS,
    )
    interfaces = interfaces_response.json()

    prefixed_interface = next(
        (iface for iface in interfaces if iface["name"] == "imported_base_interface"),
        None,
    )
    assert prefixed_interface is not None


@pytest.mark.anyio
async def test_import_project_template_with_valid_schema_overwrite_existing(
    client: AsyncClient,
):
    """Test importing project template with valid schema and overwrite existing"""
    target_project = "template_import_overwrite_target"
    await _create_project(client, target_project)

    # Create existing interface
    existing_interface_response = await _create_test_interface(
        client,
        name="existing_interface",
        project=target_project,
        color="#FF0000",
    )

    template = {
        "interfaces": [
            {
                "name": "existing_interface",
                "color": "#00FF00",  # Different color
                "tabs": [
                    {
                        "name": "new_tab",
                        "tiles": [],
                    },
                ],
            },
        ],
        "template_version": "1.0",
    }

    # First try without overwrite (should fail or skip)
    import_request = {
        "project": target_project,
        "template": template,
        "overwrite_existing": False,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/project/import_template",
        json=import_request,
        headers=HEADERS,
    )

    # Should succeed but with warnings about existing interface
    assert response.status_code == 200
    data = response.json()
    assert len(data.get("warnings", [])) > 0

    # Now try with overwrite
    import_request["overwrite_existing"] = True

    response = await client.post(
        "/v0/project/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200


@pytest.mark.anyio
async def test_import_project_template_with_valid_schema_multiple_interfaces(
    client: AsyncClient,
):
    """Test importing project template with valid schema containing multiple interfaces"""
    target_project = "template_import_multiple_target"
    await _create_project(client, target_project)

    template = {
        "interfaces": [
            {
                "name": "dashboard_interface",
                "color": "#FF0000",
                "tabs": [
                    {
                        "name": "overview_tab",
                        "active": True,
                        "tiles": [
                            {
                                "name": "summary_table",
                                "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                                "type": "Table",
                                "table_tile": {"table_type": "advanced"},
                            },
                        ],
                    },
                ],
            },
            {
                "name": "analytics_interface",
                "color": "#00FF00",
                "tabs": [
                    {
                        "name": "charts_tab",
                        "tiles": [
                            {
                                "name": "trend_plot",
                                "position": {"x": 0, "y": 0, "width": 8, "height": 6},
                                "type": "Plot",
                                "plot_tile": {
                                    "plot_type": "line",
                                    "x_axis": "time",
                                    "y_axis": "value",
                                },
                            },
                        ],
                    },
                ],
            },
        ],
        "template_version": "1.0",
    }

    import_request = {
        "project": target_project,
        "template": template,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/project/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["interfaces"] == 2
    assert data["import_stats"]["tabs"] == 2
    assert data["import_stats"]["tiles"] == 2

    # Verify both interfaces were created
    interfaces_response = await client.get(
        f"/v0/interfaces/list?project={target_project}",
        headers=HEADERS,
    )
    interfaces = interfaces_response.json()

    interface_names = [iface["name"] for iface in interfaces]
    assert "dashboard_interface" in interface_names
    assert "analytics_interface" in interface_names


@pytest.mark.anyio
async def test_export_import_project_template_with_valid_schema_roundtrip(
    client: AsyncClient,
):
    """Test exporting and then importing a project template with valid schema (roundtrip)"""
    # Create source project with complex structure
    source_project = "template_roundtrip_source"
    target_project = "template_roundtrip_target"

    await _create_project(client, source_project)
    await _create_project(client, target_project)

    # Create complex interface structure
    interface_response = await _create_test_interface(
        client,
        name="complex_interface",
        project=source_project,
        color="#FF00FF",
    )
    interface_id = interface_response.json()["id"]

    # Create multiple tabs with different tile types
    tab1_response = await _create_test_tab(
        client,
        interface_id,
        name="data_tab",
        order=0,
    )
    tab1_id = tab1_response.json()["id"]

    tab2_response = await _create_test_tab(
        client,
        interface_id,
        name="viz_tab",
        order=1,
    )
    tab2_id = tab2_response.json()["id"]

    # Add various tile types
    await _create_test_table_tile(
        client,
        tab1_id,
        name="data_table",
        table_type="advanced",
        column_context='["col1", "col2"]',
    )
    await _create_test_plot_tile(
        client,
        tab1_id,
        name="scatter_plot",
        plot_type="scatter",
        x_axis="x",
        y_axis="y",
    )
    await _create_test_view_tile(
        client,
        tab2_id,
        name="markdown_view",
        base_index="markdown",
    )
    await _create_test_editor_tile(
        client,
        tab2_id,
        name="code_editor",
        file_type="python",
        content="print('hello')",
    )
    await _create_test_terminal_tile(
        client,
        tab2_id,
        name="bash_terminal",
        shell_type="bash",
    )

    # Export the template
    export_request = {
        "project": source_project,
        "include_metadata": True,
        "description": "Roundtrip test template",
        "tags": ["roundtrip", "test"],
    }

    export_response = await client.post(
        "/v0/project/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert export_response.status_code == 200
    exported_template = export_response.json()["template"]

    # Import the template to target project
    import_request = {
        "project": target_project,
        "template": exported_template,
        "validate_first": False,
        "auto_sanitize": False,
    }

    import_response = await client.post(
        "/v0/project/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert import_response.status_code == 200
    import_data = import_response.json()

    assert import_data["success"] is True
    assert import_data["import_stats"]["interfaces"] == 1
    assert import_data["import_stats"]["tabs"] == 2
    assert import_data["import_stats"]["tiles"] == 5

    # Verify the imported structure matches
    target_interfaces_response = await client.get(
        f"/v0/interfaces/list?project={target_project}",
        headers=HEADERS,
    )
    target_interfaces = target_interfaces_response.json()

    imported_interface = target_interfaces[0]
    assert imported_interface["name"] == "complex_interface"
    assert imported_interface["color"] == "#FF00FF"

    # Verify tabs and tiles were imported correctly
    tabs_response = await client.get(
        f"/v0/tab/list?interface_id={imported_interface['id']}",
        headers=HEADERS,
    )
    tabs = tabs_response.json()
    assert len(tabs) == 2

    # Check tiles in each tab
    for tab in tabs:
        tiles_response = await client.get(
            f"/v0/tile/list?tab_id={tab['id']}",
            headers=HEADERS,
        )
        tiles = tiles_response.json()

        if tab["name"] == "data_tab":
            assert len(tiles) == 2  # table and plot
            tile_types = [tile["type"] for tile in tiles]
            assert "Table" in tile_types
            assert "Plot" in tile_types
        elif tab["name"] == "viz_tab":
            assert len(tiles) == 3  # view, editor, terminal
            tile_types = [tile["type"] for tile in tiles]
            assert "View" in tile_types
            assert "Editor" in tile_types
            assert "Terminal" in tile_types


@pytest.mark.anyio
async def test_export_project_template_with_valid_schema_empty_project(
    client: AsyncClient,
):
    """Test exporting project template with valid schema from empty project"""
    empty_project = "template_export_empty"
    await _create_project(client, empty_project)

    export_request = {
        "project": empty_project,
        "include_metadata": True,
    }

    response = await client.post(
        "/v0/project/export_template",
        json=export_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    template = data["template"]
    assert len(template["interfaces"]) == 0
    assert data["export_stats"]["interfaces"] == 0
    assert data["export_stats"]["tabs"] == 0
    assert data["export_stats"]["tiles"] == 0


@pytest.mark.anyio
async def test_import_project_template_with_valid_schema_empty_template(
    client: AsyncClient,
):
    """Test importing project template with valid schema containing empty template"""
    target_project = "template_import_empty_target"
    await _create_project(client, target_project)

    empty_template = {
        "interfaces": [],
        "template_version": "1.0",
        "description": "Empty template",
    }

    import_request = {
        "project": target_project,
        "template": empty_template,
        "validate_first": False,
        "auto_sanitize": False,
    }

    response = await client.post(
        "/v0/project/import_template",
        json=import_request,
        headers=HEADERS,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["import_stats"]["interfaces"] == 0
    assert data["import_stats"]["tabs"] == 0
    assert data["import_stats"]["tiles"] == 0


@pytest.mark.anyio
async def test_projects_tree(client: AsyncClient):
    """GET /v0/projects/tree returns list with project icons and interfaces."""
    # Create project and interface using helper functions
    project_name = "tree-proj"
    await _create_project(client, project_name)
    # create an interface via existing helper
    from .test_interface import _create_test_interface as _create_ifc

    await _create_ifc(client, name="iface1", project=project_name)

    resp = await client.get("/v0/projects/tree", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    match = next((p for p in data if p["project"] == project_name), None)
    assert match is not None
    assert match["icon"] == "folder"

    # interfaces should be list of dicts with name/icon/order
    iface = next((i for i in match["interfaces"] if i["name"] == "iface1"), None)
    assert iface is not None
    assert iface["icon"] == "folder"
    # iface tabs list should exist (empty)
    assert isinstance(iface["tabs"], list)


if __name__ == "__main__":
    pass
