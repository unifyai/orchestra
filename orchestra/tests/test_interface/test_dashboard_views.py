import os

import pytest
from httpx import AsyncClient, Request

# Common headers and data
api_key = str(os.getenv("ORCHESTRA_ADMIN_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


# Helper functions
def _create_dashboard_view(client, dashboard_view_name):
    return client.post(
        "/v0/admin/dashboard_view",
        json={"project_id": 1, "name": dashboard_view_name, "view": "test_url"},
        headers=HEADERS,
    )


def _create_project(client, project_name):
    api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
    _headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    url = "/v0/project"
    project_data = {"name": project_name}
    return client.post(url, json=project_data, headers=_headers)


@pytest.mark.anyio
async def test_list_dashboard_views(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    dashboard_view_name = "dir/subdir/dashboard_view1"
    await _create_dashboard_view(client, dashboard_view_name)

    response = await client.get("/v0/admin/dashboard_views/1", headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)
    assert response.json()[0] == ["dir/subdir/dashboard_view1", "test_url"]


@pytest.mark.anyio
async def test_rename_dashboard_views(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    dashboard_view_name = "dir/subdir/dashboard_view1"
    await _create_dashboard_view(client, dashboard_view_name)

    response = await client.patch(
        "/v0/admin/dashboard_view",
        json={"project_id": 1, "name": dashboard_view_name, "new_name": "renamed"},
        headers=HEADERS,
    )

    response = await client.get("/v0/admin/dashboard_views/1", headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)
    assert response.json()[0] == ["renamed", "test_url"]


@pytest.mark.anyio
async def test_create_dashboard_view(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    dashboard_view_name = "dir/subdir/new_dashboard_view"
    response = await _create_dashboard_view(client, dashboard_view_name)
    assert response.status_code == 201, response.json()
    assert response.json() == {"info": "DashboardView created successfully!"}


@pytest.mark.anyio
async def test_create_existing_dashboard_view(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    dashboard_view_name = "dir/subdir/existing_dashboard_view"
    await _create_dashboard_view(client, dashboard_view_name)

    # Try to create the same dashboard_view again
    response = await _create_dashboard_view(client, dashboard_view_name)
    assert response.status_code == 400, response.json()
    assert response.json() == {"detail": "DashboardView already exists"}


@pytest.mark.anyio
async def test_delete_dashboard_view(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    dashboard_view_name = "dir/subdir/dashboard_view_to_delete"
    await _create_dashboard_view(client, dashboard_view_name)

    request = Request(
        "DELETE",
        str(client.base_url) + "/v0/admin/dashboard_view",
        json={
            "project_id": 1,
            "name": dashboard_view_name,
        },
        headers=HEADERS,
    )
    response = await client.send(request)
    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "DashboardView deleted successfully!"}


@pytest.mark.anyio
async def test_delete_dashboard_view_not_found(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    dashboard_view_name = "dir/subdir/nonexistent_dashboard_view"
    request = Request(
        "DELETE",
        str(client.base_url) + "/v0/admin/dashboard_view",
        json={
            "project_id": 1,
            "name": dashboard_view_name,
        },
        headers=HEADERS,
    )
    response = await client.send(request)
    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "DashboardView not found."}
