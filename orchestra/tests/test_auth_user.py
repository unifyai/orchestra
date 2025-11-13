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
        "image": "http://...",
        "timezone": "America/New_York",
    }

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    user_data = response.json()
    assert user_data["email"] == "testuser@example.com"
    assert user_data["timezone"] == "America/New_York"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_timezone",
    ["foo", "UTC+1", "America/Fake_City", "PST"],
)
async def test_create_user_with_invalid_timezone(
    client: AsyncClient,
    invalid_timezone: str,
):
    url = "/v0/admin/auth-user"
    params = {"email": "tz_fail@example.com", "timezone": invalid_timezone}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 422, response.json()
    assert "timezone" in response.json()["detail"][0]["loc"]
    assert "not a valid IANA timezone" in response.json()["detail"][0]["msg"]


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
    params = {"email": "testuser4@example.com", "timezone": "Europe/London"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # update
    url = "/v0/admin/auth-user"
    params = {
        "user_id": user_id,
        "name": "A",
        "last_name": "B",
        "timezone": "Asia/Tokyo",
    }
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # check updated info
    url = f"/v0/admin/auth-user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["name"] == "A"
    assert response.json()["timezone"] == "Asia/Tokyo"


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
async def test_freeze_account_by_stripe_id(client: AsyncClient):
    # Create a user
    url = "/v0/admin/auth-user"
    email = "testfreeze@example.com"
    params = {"email": email}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    user_id = response.json()["id"]

    # Set a stripe_id for the user
    stripe_id = "stripe_test_123"
    url = "/v0/admin/auth-user/stripe-id"
    params = {"user_id": user_id, "stripe_id": stripe_id}
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # Freeze the account by stripe_id
    url = "/v0/admin/auth-user/freeze-by-stripe-id"
    params = {"stripe_id": stripe_id, "freeze": True}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # Check if the account is frozen
    url = f"/v0/admin/auth-user/is-frozen?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["is_frozen"] is True

    # Unfreeze the account
    url = "/v0/admin/auth-user/freeze-by-stripe-id"
    params = {"stripe_id": stripe_id, "freeze": False}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # Check if the account is unfrozen
    url = f"/v0/admin/auth-user/is-frozen?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["is_frozen"] is False


@pytest.mark.anyio
async def test_create_api_key(client: AsyncClient):
    url = "/v0/admin/api_key"
    params = {"name": "test_key", "user_id": "test_user_id"}

    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_reset_api_key(client: AsyncClient):
    url = "/v0/admin/auth-user"
    params = {"email": "testuser@api_key.com"}
    response = await client.post(url, json=params, headers=HEADERS)

    url = f"/v0/admin/auth-user/by-email?email=testuser@api_key.com"
    response = await client.get(url, headers=HEADERS)
    user_id = response.json()["id"]
    old_api_key = response.json()["apiKey"]

    url = f"/v0/admin/api_key/reset"
    response = await client.post(url, params={"user_id": user_id}, headers=HEADERS)
    new_api_key_in_response = response.json()

    url = f"/v0/admin/auth-user/by-email?email=testuser@api_key.com"
    response = await client.get(url, headers=HEADERS)
    new_api_key_in_db = response.json()["apiKey"]

    assert new_api_key_in_response == new_api_key_in_db
    assert new_api_key_in_db != old_api_key


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
    email = "user@org1.co"
    params = {"email": email}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]
    # create org
    url = "/v0/admin/organization"
    params = {"name": "Org1", "owner_id": owner_id}
    response = await client.post(url, params=params, headers=HEADERS)

    # check member with mail
    url = f"/v0/admin/auth-user/by-email?email={email}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["organization"]["name"] == None
    assert response.json()["organization"]["level"] == None

    # add member to org
    url = "/v0/admin/organization/member"
    params = {"name": "Org1", "new_member_email": email}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # check member with mail
    url = f"/v0/admin/auth-user/by-email?email={email}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["organization"]["name"] == "Org1"
    assert response.json()["organization"]["level"] == "user"

    # check member with user id
    url = f"/v0/admin/auth-user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["id"] == user_id
    assert response.json()["organization"]["name"] == "Org1"
    assert response.json()["organization"]["level"] == "user"

    # update user level within the org
    url = "/v0/admin/organization/member/level"
    params = {"organization": "Org1", "member_email": email, "new_level": "admin"}
    response = await client.put(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # check member with mail
    url = f"/v0/admin/auth-user/by-email?email={email}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["organization"]["name"] == "Org1"
    assert response.json()["organization"]["level"] == "admin"

    # list users in a given org
    url = f"/v0/admin/organization/list?name=Org1"
    response = await client.get(url, headers=HEADERS)
    expected_result = [
        {"email": "owner@org1.com", "level": "owner"},
        {"email": "user@org1.co", "level": "admin"},
    ]
    assert response.json() == expected_result


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


@pytest.mark.anyio
async def test_default_unity_resources_on_user_creation(client: AsyncClient):
    url = "/v0/admin/auth-user"
    params = {"email": "unity_resources_test@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # get the user id from the response
    user_id = response.json()["id"]

    # call the get_user endpoint with the user id
    url = f"/v0/admin/auth-user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # get the api key from the response
    api_key = response.json()["apiKey"]

    # Create user-specific headers with the API key
    user_headers = {"accept": "application/json", "Authorization": f"Bearer {api_key}"}

    # 1. Verify the Unity project exists
    response = await client.get("/v0/projects", headers=user_headers)
    assert response.status_code == 200, response.json()
    projects = response.json()
    assert "Unity" in projects, "Unity project not found in user projects"

    # 2. Verify the Unity interface exists
    response = await client.get(
        "/v0/interfaces/list?project=Unity",
        headers=user_headers,
    )
    assert response.status_code == 200, response.json()
    interfaces = response.json()
    unity_interface = next(
        (interface for interface in interfaces if interface["name"] == "Unity"),
        None,
    )
    assert unity_interface is not None, "Unity interface not found"
    unity_interface_id = unity_interface["id"]

    # 3. Verify the Tasks tab exists
    response = await client.get(
        f"/v0/tab/list?interface_id={unity_interface_id}",
        headers=user_headers,
    )
    assert response.status_code == 200, response.json()
    tabs = response.json()
    tasks_tab = next((tab for tab in tabs if tab["name"] == "Tasks"), None)
    assert tasks_tab is not None, "Tasks tab not found"
    tasks_tab_id = tasks_tab["id"]

    # 4. Verify the Tasks table tile exists
    response = await client.get(
        f"/v0/tile/list?tab_id={tasks_tab_id}",
        headers=user_headers,
    )
    assert response.status_code == 200, response.json()
    tiles = response.json()
    tasks_tile = next(
        (
            tile
            for tile in tiles
            if tile["type"] == "Table"
            and tile["context"] == "Tasks"
            and tile["name"] == "Tasks"
        ),
        None,
    )
    assert tasks_tile is not None, "Tasks table tile not found"


@pytest.mark.anyio
async def test_onboarding_status_workflow(client: AsyncClient):
    """
    Test the full onboarding status workflow:
    1. Create a user.
    2. Get initial status (should be False).
    3. Update status to True.
    4. Verify updated status.
    5. Verify status is included in main user endpoint.
    """
    # 1. Create a test user and get their API key
    create_response = await client.post(
        "/v0/admin/auth-user",
        json={"email": "onboarding@test.com"},
        headers=HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()
    user_id = create_response.json()["id"]

    user_info_response = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert user_info_response.status_code == 200, user_info_response.json()
    api_key = user_info_response.json()["apiKey"]
    user_headers = {"Authorization": f"Bearer {api_key}"}

    # 2. Get initial onboarding status
    response = await client.get("/v0/user/onboarding-status", headers=user_headers)
    assert response.status_code == 200
    assert response.json() == {"onboarded": False}

    # 3. Update onboarding status to True
    update_payload = {"onboarded": True}
    response = await client.put(
        "/v0/user/onboarding-status",
        json=update_payload,
        headers=user_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"message": "Onboarding status updated successfully"}

    # 4. Verify updated status
    response = await client.get("/v0/user/onboarding-status", headers=user_headers)
    assert response.status_code == 200
    assert response.json() == {"onboarded": True}

    # 5. Verify onboarding status is included in admin user info endpoint
    response = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["onboarded"] is True

    # 6. Test updating back to False
    update_payload = {"onboarded": False}
    response = await client.put(
        "/v0/user/onboarding-status",
        json=update_payload,
        headers=user_headers,
    )
    assert response.status_code == 200

    response = await client.get("/v0/user/onboarding-status", headers=user_headers)
    assert response.status_code == 200
    assert response.json() == {"onboarded": False}


if __name__ == "__main__":
    pass
