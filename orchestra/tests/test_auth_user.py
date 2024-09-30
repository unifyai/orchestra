import os

import pytest
from httpx import AsyncClient

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
}


@pytest.mark.anyio
async def test_create_user(client: AsyncClient):
    url = "/v0/admin/auth-user"
    params = {
        "email": "testuser@example.com",
        "name": "Test User",
        "job_title": "Developer",
    }

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    user_data = response.json()
    assert user_data["email"] == "testuser@example.com"


@pytest.mark.anyio
async def test_get_user_by_user_id(client: AsyncClient):
    url = "/v0/admin/auth-user"
    params = {"email": "testuser2@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    url = f"/v0/admin/auth-user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["id"] == user_id


@pytest.mark.anyio
async def test_get_user_by_email(client: AsyncClient):
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "testuser3@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    email = response.json()["email"]

    url = f"/v0/admin/auth-user/by-email?email={email}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["email"] == email


@pytest.mark.anyio
async def test_update_user(client: AsyncClient):
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "testuser4@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # update
    url = "/v0/admin/auth-user"
    params = {"user_id": user_id, "name": "A", "last_name": "B"}
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # check updated info
    url = f"/v0/admin/auth-user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["name"] == "A"


@pytest.mark.anyio
async def test_delete_user(client: AsyncClient):
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "testuser5@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # check it's there
    url = f"/v0/admin/auth-user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["id"] == user_id

    # delete
    url = f"/v0/admin/auth-user?user_id={user_id}"
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # check it has been deleted
    url = f"/v0/admin/auth-user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 404, response.json()


@pytest.mark.anyio
async def test_link_account(client: AsyncClient):
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "testuser6@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    url = "/v0/admin/account"
    params = {
        "userId": user_id,
        "type": "oauth",
        "provider": "google",
        "providerAccountId": "12345",
        "access_token": "test_access_token",
        "expires_at": 1234567890,
        "scope": "oauth",
        "token_type": "...",
        "id_token": "...",
    }

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_set_user_tier(client: AsyncClient):
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "testuser7@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # update tier
    url = "/v0/admin/auth-user/tier"
    params = {"user_id": user_id, "tier": "professional"}
    response = await client.put(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # TODO: Check that the tier has changed


@pytest.mark.anyio
async def test_reset_user_quotas(client: AsyncClient):
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "testuser8@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # TODO: Deactivate quotas explicitly

    # reset quotas
    url = f"/v0/admin/auth-user/quotas/reset?user_id={user_id}"
    response = await client.put(url, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # TODO: Check that quotas are restarted


@pytest.mark.anyio
async def test_create_api_key(client: AsyncClient):
    url = "/v0/admin/api_key"
    params = {"name": "test_key", "user_id": "test_user_id"}

    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_reset_api_key(client: AsyncClient):
    url = "/v0/admin/auth-user"
    params = {"email": "testuser@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    url = "/v0/admin/api_key/reset"
    params = {"user_id": user_id}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_create_organization(client: AsyncClient):
    # create owner
    url = "/v0/admin/auth-user"
    params = {"email": "owner@org0.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    url = "/v0/admin/organization"
    params = {"name": "org0", "owner_id": user_id}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_create_organization_existing(client: AsyncClient):
    # create owner
    url = "/v0/admin/auth-user"
    params = {"email": "owner@org01.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # create first org
    url = "/v0/admin/organization"
    params = {"name": "org01", "owner_id": user_id}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create second org
    params = {"name": "org02", "owner_id": user_id}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 400, response.json()


@pytest.mark.anyio
async def test_add_organization_member(client: AsyncClient):
    # create owner
    url = "/v0/admin/auth-user"
    params = {"email": "owner@org1.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    owner_id = response.json()["id"]
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "user@org1.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    # create org
    url = "/v0/admin/organization"
    params = {"name": "Org1", "owner_id": owner_id}
    response = await client.post(url, params=params, headers=HEADERS)

    # add member to org
    url = "/v0/admin/organization/member"
    params = {"name": "Org1", "new_member_email": "user@org1.com"}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_add_organization_member_user_not_found(client: AsyncClient):
    # create owner
    url = "/v0/admin/auth-user"
    params = {"email": "owner@organization.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    owner_id = response.json()["id"]
    # create org
    url = "/v0/admin/organization"
    params = {"name": "Org123", "owner_id": owner_id}
    response = await client.post(url, params=params, headers=HEADERS)

    # add non existing user to a valid org
    url = "/v0/admin/organization/member"
    params = {"name": "Org123", "new_member_email": "fake_user@org.com"}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 404, response.json()


@pytest.mark.anyio
async def test_add_organization_member_org_not_found(client: AsyncClient):
    # create user
    url = "/v0/admin/auth-user"
    params = {"email": "newmember@organization.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    email = response.json()["email"]

    # add user to a fake org
    url = "/v0/v0/admin/organization/member"
    params = {"name": "Not an org", "new_member_email": email}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 404, response.json()
