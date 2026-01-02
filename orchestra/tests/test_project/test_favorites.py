import pytest
from httpx import AsyncClient

from .test_interface import _create_project
from .test_projects import HEADERS


@pytest.mark.anyio
async def test_get_empty_favorites(client: AsyncClient):
    """
    GET /v0/project/favorites should return an empty list when no favorites exist.
    """
    response = await client.get("/v0/project/favorites", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.anyio
async def test_create_favorite(client: AsyncClient):
    """
    POST /v0/project/favorites should create a favorite and return the created object.
    """
    project_name = "proj1"
    await _create_project(client, project_name)

    payload = {"project": project_name, "position": 1}
    resp = await client.post("/v0/project/favorites", json=payload, headers=HEADERS)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "id" in data and isinstance(data["id"], int)
    assert data["project"] == project_name
    assert data["position"] == 1


@pytest.mark.anyio
async def test_get_favorite_by_id(client: AsyncClient):
    """
    GET /v0/project/favorites/{id} should return the favorite when it exists.
    """
    project_name = "proj2"
    await _create_project(client, project_name)
    payload = {"project": project_name, "position": 2}
    post_resp = await client.post(
        "/v0/project/favorites",
        json=payload,
        headers=HEADERS,
    )
    fav_id = post_resp.json()["id"]

    get_resp = await client.get(f"/v0/project/favorites/{fav_id}", headers=HEADERS)
    assert get_resp.status_code == 200
    fav = get_resp.json()
    assert fav["id"] == fav_id
    assert fav["project"] == project_name
    assert fav["position"] == 2


@pytest.mark.anyio
async def test_get_favorite_not_found(client: AsyncClient):
    """
    GET /v0/project/favorites/{id} for a non-existent id should return 404.
    """
    resp = await client.get("/v0/project/favorites/9999", headers=HEADERS)
    assert resp.status_code == 404
    assert "Favorite with ID 9999 not found" in resp.json().get("detail", "")


@pytest.mark.anyio
async def test_update_favorite_icon_and_position(client: AsyncClient):
    """
    PATCH /v0/project/favorites/{id} should update icon and position.
    """
    project_name = "proj3"
    await _create_project(client, project_name)
    payload = {"project": project_name, "position": 3}
    post_resp = await client.post(
        "/v0/project/favorites",
        json=payload,
        headers=HEADERS,
    )
    fav_id = post_resp.json()["id"]

    update_payload = {"position": 4}
    patch_resp = await client.patch(
        f"/v0/project/favorites/{fav_id}",
        json=update_payload,
        headers=HEADERS,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()
    assert updated["id"] == fav_id
    assert updated["position"] == 4

    get_resp = await client.get(f"/v0/project/favorites/{fav_id}", headers=HEADERS)
    assert get_resp.status_code == 200
    fav = get_resp.json()
    assert fav["position"] == 4


@pytest.mark.anyio
async def test_update_favorite_not_found(client: AsyncClient):
    """
    PATCH /v0/project/favorites/{id} for a non-existent id should return 404.
    """
    update_payload = {"position": 1}
    resp = await client.patch(
        "/v0/project/favorites/8888",
        json=update_payload,
        headers=HEADERS,
    )
    assert resp.status_code == 404
    assert "Favorite with ID 8888 not found" in resp.json().get("detail", "")


@pytest.mark.anyio
async def test_delete_favorite(client: AsyncClient):
    """
    DELETE /v0/project/favorites/{id} should delete the favorite.
    """
    project_name = "proj4"
    await _create_project(client, project_name)
    payload = {"project": project_name, "position": 5}
    post_resp = await client.post(
        "/v0/project/favorites",
        json=payload,
        headers=HEADERS,
    )
    fav_id = post_resp.json()["id"]

    del_resp = await client.delete(f"/v0/project/favorites/{fav_id}", headers=HEADERS)
    assert del_resp.status_code == 200

    list_resp = await client.get("/v0/project/favorites", headers=HEADERS)
    assert list_resp.status_code == 200
    assert all(f["id"] != fav_id for f in list_resp.json())

    get_resp = await client.get(f"/v0/project/favorites/{fav_id}", headers=HEADERS)
    assert get_resp.status_code == 404


@pytest.mark.anyio
async def test_delete_favorite_not_found(client: AsyncClient):
    """
    DELETE /v0/project/favorites/{id} for a non-existent id should return 404.
    """
    resp = await client.delete("/v0/project/favorites/7777", headers=HEADERS)
    assert resp.status_code == 404
    assert "Favorite with ID 7777 not found" in resp.json().get("detail", "")


@pytest.mark.anyio
async def test_nonexistent_project_on_create(client: AsyncClient):
    """
    Posting a favorite for a non-existent project should return 404.
    """
    payload = {"project": "no_proj", "position": 2}
    resp = await client.post("/v0/project/favorites", json=payload, headers=HEADERS)
    assert resp.status_code == 404
    assert "Project 'no_proj' not found" in resp.json().get("detail", "")
