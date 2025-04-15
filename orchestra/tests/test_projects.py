import os

import pytest
from httpx import AsyncClient

from .test_legacy_interface import _create_context, _create_interface, _create_project
from .test_log import _create_derived_entry, _create_log, _update_logs

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
    Test the admin endpoint for duplicating a project.
    This test verifies that an admin can create a deep copy of a project from one user to another.
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
            "email": "test_duplicate_target@example.com",
            "name": "test_duplicate_target",
        },
        headers=admin_headers,
    )
    data = response.json()
    target_user_id = data["id"]
    source_user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

    # Create a source project with contexts, interfaces, and logs
    source_project_name = "source_project_for_duplication"
    target_project_name = "duplicated_project"
    context_name = "source_test_context"
    interface_name = "source_test_interface"

    # Create project and its components
    await _create_project(client, source_project_name)
    await _create_context(client, source_project_name, context_name, "test description")
    await _create_context(client, source_project_name, "dummy-context", None)

    # Create interface
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
    await _create_interface(
        client,
        interface_name,
        source_project_name,
        items,
        new_counter,
    )

    # Create logs
    log_id = await _create_log(
        client,
        source_project_name,
        context={"name": context_name},
        entries={"source_key": "source_value"},
    )

    # create a derived log
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
    assert result["interfaces_copied"] >= 1
    assert result["logs_copied"] >= 1
    assert result["derived_logs_copied"] >= 1

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

    # 3) Verify the target user can access the project's interfaces
    response = await client.get(
        f"/v0/interface",
        params={"project": target_project_name},
        headers=target_headers,
    )
    assert response.status_code == 200, response.json()
    interfaces = response.json()
    assert len(interfaces) > 0
    assert interface_name in [interface["name"] for interface in interfaces]

    # 4) Verify the target user can access the project's logs
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

    # 5) Update the source project logs and verify the changes don't affect the duplicate
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


if __name__ == "__main__":
    pass
