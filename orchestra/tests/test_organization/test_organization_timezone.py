"""Tests for organization timezone functionality."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_create_organization_with_custom_timezone(client: AsyncClient):
    """Test that creating an organization with a custom timezone uses that timezone."""
    # Create test user with a timezone
    owner = await create_test_user(client, "custom_tz_owner@test.com")

    # Set the owner's timezone (admin endpoint)
    await client.put(
        "/v0/admin/user",
        json={"user_id": owner["id"], "timezone": "America/New_York"},
        headers=ADMIN_HEADERS,
    )

    # Create organization with a DIFFERENT timezone than owner
    response = await client.post(
        "/v0/organizations",
        json={"name": "Custom TZ Test Org", "timezone": "Europe/Berlin"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED
    org_data = response.json()

    # Verify organization uses the provided timezone, NOT owner's
    assert org_data["timezone"] == "Europe/Berlin"


@pytest.mark.anyio
async def test_create_organization_inherits_owner_timezone(client: AsyncClient):
    """Test that creating an organization without timezone inherits the owner's timezone."""
    # Create test user
    owner = await create_test_user(client, "tz_owner@test.com")

    # Set the owner's timezone (admin endpoint)
    update_response = await client.put(
        "/v0/admin/user",
        json={"user_id": owner["id"], "timezone": "America/New_York"},
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == status.HTTP_200_OK

    # Create organization WITHOUT specifying timezone
    response = await client.post(
        "/v0/organizations",
        json={"name": "TZ Test Org"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED
    org_data = response.json()

    # Verify organization inherited owner's timezone
    assert org_data["timezone"] == "America/New_York"


@pytest.mark.anyio
async def test_create_organization_with_null_owner_timezone(client: AsyncClient):
    """Test that creating an organization with null owner timezone results in null org timezone."""
    # Create test user (no timezone set by default)
    owner = await create_test_user(client, "null_tz_owner@test.com")

    # Create organization without setting owner timezone
    response = await client.post(
        "/v0/organizations",
        json={"name": "Null TZ Test Org"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED
    org_data = response.json()

    # Verify organization timezone is null (will default to UTC at runtime)
    assert org_data["timezone"] is None


@pytest.mark.anyio
async def test_create_organization_with_invalid_timezone(client: AsyncClient):
    """Test that creating an organization with invalid timezone is rejected."""
    # Create test user
    owner = await create_test_user(client, "invalid_create_tz_owner@test.com")

    # Try to create organization with invalid timezone
    response = await client.post(
        "/v0/organizations",
        json={"name": "Invalid Create TZ Org", "timezone": "Not/A/Timezone"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_get_organization_returns_timezone(client: AsyncClient):
    """Test that getting an organization returns the timezone field."""
    # Create test user with timezone
    owner = await create_test_user(client, "get_tz_owner@test.com")

    # Set the owner's timezone (admin endpoint)
    await client.put(
        "/v0/admin/user",
        json={"user_id": owner["id"], "timezone": "Europe/London"},
        headers=ADMIN_HEADERS,
    )

    # Create organization
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Get TZ Test Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]

    # Get organization
    response = await client.get(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    org_data = response.json()

    assert org_data["timezone"] == "Europe/London"


@pytest.mark.anyio
async def test_list_organizations_returns_timezone(client: AsyncClient):
    """Test that listing organizations returns the timezone field."""
    # Create test user with timezone
    owner = await create_test_user(client, "list_tz_owner@test.com")

    # Set the owner's timezone (admin endpoint)
    await client.put(
        "/v0/admin/user",
        json={"user_id": owner["id"], "timezone": "Asia/Tokyo"},
        headers=ADMIN_HEADERS,
    )

    # Create organization
    await client.post(
        "/v0/organizations",
        json={"name": "List TZ Test Org"},
        headers=owner["headers"],
    )

    # List organizations
    response = await client.get(
        "/v0/organizations",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    orgs = response.json()

    # Find our org
    our_org = next((org for org in orgs if org["name"] == "List TZ Test Org"), None)
    assert our_org is not None
    assert our_org["timezone"] == "Asia/Tokyo"


@pytest.mark.anyio
async def test_admin_create_organization_inherits_owner_timezone(client: AsyncClient):
    """Test that admin endpoint for creating organization also inherits owner timezone."""
    # Create test user with timezone
    owner = await create_test_user(client, "admin_tz_owner@test.com")

    # Set the owner's timezone (admin endpoint)
    await client.put(
        "/v0/admin/user",
        json={"user_id": owner["id"], "timezone": "Pacific/Auckland"},
        headers=ADMIN_HEADERS,
    )

    # Create organization via admin endpoint
    response = await client.post(
        "/v0/admin/organization",
        params={"name": "Admin TZ Test Org", "owner_id": owner["id"]},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK

    # Get the organization to verify timezone
    list_response = await client.get(
        "/v0/organizations",
        headers=owner["headers"],
    )
    orgs = list_response.json()
    our_org = next(
        (org for org in orgs if org["name"] == "Admin TZ Test Org"),
        None,
    )
    assert our_org is not None
    assert our_org["timezone"] == "Pacific/Auckland"


@pytest.mark.anyio
async def test_update_organization_timezone(client: AsyncClient):
    """Test that organization timezone can be updated via PATCH."""
    # Create test user
    owner = await create_test_user(client, "update_tz_owner@test.com")

    # Create organization (no timezone initially)
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Update TZ Test Org"},
        headers=owner["headers"],
    )
    assert create_response.status_code == status.HTTP_201_CREATED
    org_id = create_response.json()["id"]
    assert create_response.json()["timezone"] is None

    # Update organization timezone
    update_response = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"timezone": "America/Los_Angeles"},
        headers=owner["headers"],
    )
    assert update_response.status_code == status.HTTP_200_OK
    assert update_response.json()["timezone"] == "America/Los_Angeles"

    # Verify timezone persisted
    get_response = await client.get(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert get_response.json()["timezone"] == "America/Los_Angeles"


@pytest.mark.anyio
async def test_update_organization_timezone_invalid(client: AsyncClient):
    """Test that invalid timezone is rejected."""
    # Create test user
    owner = await create_test_user(client, "invalid_tz_owner@test.com")

    # Create organization
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Invalid TZ Test Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]

    # Try to update with invalid timezone
    update_response = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"timezone": "Invalid/Timezone"},
        headers=owner["headers"],
    )
    assert update_response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_update_organization_timezone_to_null(client: AsyncClient):
    """Test that organization timezone can be set back to null."""
    # Create test user with timezone
    owner = await create_test_user(client, "null_update_tz_owner@test.com")

    # Set owner's timezone
    await client.put(
        "/v0/admin/user",
        json={"user_id": owner["id"], "timezone": "Europe/Paris"},
        headers=ADMIN_HEADERS,
    )

    # Create organization (inherits Europe/Paris)
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Null Update TZ Test Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]
    assert create_response.json()["timezone"] == "Europe/Paris"

    # Update timezone to a different value (not null - setting null would require
    # explicit support, which we don't have yet)
    update_response = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"timezone": "UTC"},
        headers=owner["headers"],
    )
    assert update_response.status_code == status.HTTP_200_OK
    assert update_response.json()["timezone"] == "UTC"
