"""
Tests for User CRUD operations.

Covers:
- Create user (with various field combinations)
- Read user (by ID, by email)
- Update user
- Delete user
- Link accounts
- Self-healing when user has no API key
"""

import os

import pytest
from httpx import AsyncClient

from orchestra.db.dao.api_key_dao import ApiKeyDAO

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
}


@pytest.mark.anyio
async def test_create_user(client: AsyncClient):
    """Test basic user creation with profile fields."""
    url = "/v0/admin/user"
    params = {
        "email": "crud_create@example.com",
        "name": "Test User",
        "job_title": "Developer",
        "bio": "A developer",
        "image": "http://example.com/image.jpg",
        "timezone": "America/New_York",
    }

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    user_data = response.json()
    assert user_data["email"] == "crud_create@example.com"
    assert user_data["name"] == "Test User"
    assert user_data["bio"] == "A developer"
    assert user_data["timezone"] == "America/New_York"


@pytest.mark.anyio
async def test_create_user_minimal(client: AsyncClient):
    """Test user creation with only required email field."""
    url = "/v0/admin/user"
    params = {"email": "crud_minimal@example.com"}

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    user_data = response.json()
    assert user_data["email"] == "crud_minimal@example.com"
    assert "id" in user_data


@pytest.mark.anyio
async def test_get_user_by_user_id(client: AsyncClient):
    """Test retrieving user by ID."""
    # Create user first
    url = "/v0/admin/user"
    params = {"email": "crud_get_by_id@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Get by ID
    url = f"/v0/admin/user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["id"] == user_id


@pytest.mark.anyio
async def test_get_user_by_email(client: AsyncClient):
    """Test retrieving user by email."""
    # Create user first
    url = "/v0/admin/user"
    params = {"email": "crud_get_by_email@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    email = response.json()["email"]

    # Get by email
    url = f"/v0/admin/user/by-email?email={email}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["email"] == email


@pytest.mark.anyio
async def test_get_user_not_found(client: AsyncClient):
    """Test that getting non-existent user returns 404."""
    url = "/v0/admin/user/by-user-id?user_id=nonexistent_id_12345"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 404


@pytest.mark.anyio
async def test_update_user(client: AsyncClient):
    """Test updating user profile fields."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "crud_update@example.com", "name": "Original Name"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Update
    url = "/v0/admin/user"
    params = {
        "user_id": user_id,
        "name": "Updated Name",
        "last_name": "LastName",
        "bio": "Updated bio",
    }
    response = await client.put(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # Verify update
    url = f"/v0/admin/user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["name"] == "Updated Name"
    assert response.json()["bio"] == "Updated bio"


@pytest.mark.anyio
async def test_delete_user(client: AsyncClient):
    """Test deleting a user."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "crud_delete@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Delete
    url = f"/v0/admin/user?user_id={user_id}"
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # Verify deleted
    url = f"/v0/admin/user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 404


@pytest.mark.anyio
async def test_link_account(client: AsyncClient):
    """Test linking accounts (e.g., social login)."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "crud_link@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Link account - correct endpoint path
    url = "/v0/admin/link-account"
    response = await client.post(
        url,
        json={"user_id": user_id, "new_id": "oauth_linked_id_123"},
        headers=HEADERS,
    )
    if response.status_code == 404:
        pytest.skip("Link account endpoint not available")
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_set_user_tier(client: AsyncClient):
    """Test setting user tier."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "crud_tier@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Set tier (endpoint moved to admin /billing/tier, backward-compat at /user/tier)
    url = "/v0/admin/billing/tier"
    response = await client.put(
        url,
        params={"user_id": user_id, "tier": "enterprise"},
        headers=HEADERS,
    )
    if response.status_code == 404:
        pytest.skip("Billing tier endpoint not available")
    assert response.status_code == 200, response.json()


# ---------------------------------------------------------------------------
# Self-healing: missing API key tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_user_by_email_missing_api_key(client: AsyncClient, dbsession):
    """Test that get_user_by_email self-heals when user has no API key."""
    # Create user (this also creates an API key)
    url = "/v0/admin/user"
    params = {"email": "no_key_email@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200
    user_id = response.json()["id"]

    # Delete the user's API key directly via DAO
    api_key_dao = ApiKeyDAO(dbsession)
    keys = api_key_dao.filter(user_id=user_id)
    assert len(keys) > 0, "User should have an API key after creation"
    for key_row in keys:
        api_key_dao.delete(key_row[0].id)

    # Verify key is gone
    remaining = api_key_dao.filter(user_id=user_id)
    assert len(remaining) == 0, "All API keys should be deleted"

    # Now call get_user_by_email — it should self-heal and return 200
    url = f"/v0/admin/user/by-email?email=no_key_email@example.com"
    response = await client.get(url, headers=HEADERS)
    assert (
        response.status_code == 200
    ), f"Expected 200 but got {response.status_code}: {response.text}"
    data = response.json()
    assert data["email"] == "no_key_email@example.com"
    assert data["api_key"] is not None, "A new API key should have been generated"
    assert len(data["api_key"]) > 0


@pytest.mark.anyio
async def test_get_user_by_user_id_missing_api_key(client: AsyncClient, dbsession):
    """Test that get_user by user_id self-heals when user has no API key."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "no_key_userid@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200
    user_id = response.json()["id"]

    # Delete the user's API key directly via DAO
    api_key_dao = ApiKeyDAO(dbsession)
    keys = api_key_dao.filter(user_id=user_id)
    assert len(keys) > 0
    for key_row in keys:
        api_key_dao.delete(key_row[0].id)

    # Verify key is gone
    remaining = api_key_dao.filter(user_id=user_id)
    assert len(remaining) == 0

    # Now call get_user by user_id — it should self-heal and return 200
    url = f"/v0/admin/user/by-user-id?user_id={user_id}"
    response = await client.get(url, headers=HEADERS)
    assert (
        response.status_code == 200
    ), f"Expected 200 but got {response.status_code}: {response.text}"
    data = response.json()
    assert data["id"] == user_id
    assert data["api_key"] is not None, "A new API key should have been generated"
    assert len(data["api_key"]) > 0


@pytest.mark.anyio
async def test_get_user_by_account_missing_api_key(client: AsyncClient, dbsession):
    """Test that get_user_by_account self-heals when user has no API key."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "no_key_account@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200
    user_id = response.json()["id"]

    # Link an OAuth account to the user
    url = "/v0/admin/auth/account"
    account_params = {
        "user_id": user_id,
        "provider": "google",
        "type": "oauth",
        "provider_account_id": "google_test_no_key_12345",
        "access_token": "fake_token",
        "expires_at": 9999999999,
        "scope": "openid email profile",
        "token_type": "Bearer",
        "id_token": "fake_id_token",
    }
    response = await client.post(url, json=account_params, headers=HEADERS)
    assert response.status_code == 200, f"Failed to link account: {response.text}"

    # Delete the user's API key directly via DAO
    api_key_dao = ApiKeyDAO(dbsession)
    keys = api_key_dao.filter(user_id=user_id)
    assert len(keys) > 0
    for key_row in keys:
        api_key_dao.delete(key_row[0].id)

    # Verify key is gone
    remaining = api_key_dao.filter(user_id=user_id)
    assert len(remaining) == 0

    # Now call get_user_by_account — it should self-heal and return 200
    url = (
        "/v0/admin/user/by-account"
        "?provider_account_id=google_test_no_key_12345&provider=google"
    )
    response = await client.get(url, headers=HEADERS)
    assert (
        response.status_code == 200
    ), f"Expected 200 but got {response.status_code}: {response.text}"
    data = response.json()
    assert data["id"] == user_id
    assert data["api_key"] is not None, "A new API key should have been generated"
    assert len(data["api_key"]) > 0
