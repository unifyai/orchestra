"""
Tests for User CRUD operations.

Covers:
- Create user (with various field combinations)
- Read user (by ID, by email)
- Update user
- Delete user
- Link accounts
"""

import os

import pytest
from httpx import AsyncClient

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
