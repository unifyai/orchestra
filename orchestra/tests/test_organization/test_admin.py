"""
Tests for the admin organizations endpoint.
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_admin_list_organizations_empty(client: AsyncClient):
    """Test listing organizations when none exist returns empty list."""
    resp = await client.get("/v0/admin/organizations", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "organizations" in data
    assert isinstance(data["organizations"], list)
    assert data["limit"] == 100
    assert data["offset"] == 0


@pytest.mark.anyio
async def test_admin_list_organizations_basic(client: AsyncClient):
    """Test listing organizations returns correct data."""
    # Create a user who will own the organization
    user = await create_test_user(client, "org_admin_test@example.com")

    # Create an organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Admin Test Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # List organizations via admin endpoint
    resp = await client.get("/v0/admin/organizations", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()

    assert "organizations" in data
    orgs = data["organizations"]
    assert len(orgs) >= 1

    # Find our created org
    our_org = next((o for o in orgs if o["id"] == org_id), None)
    assert our_org is not None
    assert our_org["name"] == "Admin Test Org"
    assert our_org["owner_id"] == user["id"]
    assert our_org["member_count"] >= 0
    assert "created_at" in our_org


@pytest.mark.anyio
async def test_admin_list_organizations_with_members(client: AsyncClient, dbsession):
    """Test that member_count correctly reflects organization membership."""
    # Create owner and member users
    owner = await create_test_user(client, "org_owner_members@example.com")
    member1 = await create_test_user(client, "org_member1@example.com")
    member2 = await create_test_user(client, "org_member2@example.com")

    # Create an organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Members Test Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Add two members to the organization
    add_member1 = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member1["id"]},
        headers=owner["headers"],
    )
    assert add_member1.status_code == status.HTTP_201_CREATED

    add_member2 = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member2["id"]},
        headers=owner["headers"],
    )
    assert add_member2.status_code == status.HTTP_201_CREATED

    # List organizations via admin endpoint
    resp = await client.get("/v0/admin/organizations", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()

    # Find our org and verify member count (owner + 2 members = 3)
    # Note: The owner is automatically added as an OrganizationMember when the org is created
    our_org = next((o for o in data["organizations"] if o["id"] == org_id), None)
    assert our_org is not None
    assert our_org["member_count"] == 3  # owner + 2 added members


@pytest.mark.anyio
async def test_admin_list_organizations_pagination(client: AsyncClient):
    """Test pagination with limit and offset."""
    # Create a user and multiple organizations
    user = await create_test_user(client, "org_pagination@example.com")

    created_org_ids = []
    for i in range(5):
        org_resp = await client.post(
            "/v0/organizations",
            json={"name": f"Pagination Org {i}"},
            headers=user["headers"],
        )
        assert org_resp.status_code == status.HTTP_201_CREATED
        created_org_ids.append(org_resp.json()["id"])

    # Test with limit=2
    resp1 = await client.get(
        "/v0/admin/organizations?limit=2&offset=0",
        headers=ADMIN_HEADERS,
    )
    assert resp1.status_code == status.HTTP_200_OK
    data1 = resp1.json()
    assert data1["limit"] == 2
    assert data1["offset"] == 0
    assert len(data1["organizations"]) <= 2

    # Test with offset
    resp2 = await client.get(
        "/v0/admin/organizations?limit=2&offset=2",
        headers=ADMIN_HEADERS,
    )
    assert resp2.status_code == status.HTTP_200_OK
    data2 = resp2.json()
    assert data2["limit"] == 2
    assert data2["offset"] == 2

    # Ensure different results with different offsets
    ids1 = {o["id"] for o in data1["organizations"]}
    ids2 = {o["id"] for o in data2["organizations"]}
    # They should not overlap (assuming we have enough orgs)
    if len(ids1) > 0 and len(ids2) > 0:
        assert ids1 != ids2


@pytest.mark.anyio
async def test_admin_list_organizations_filter_by_name(client: AsyncClient):
    """Test filtering organizations by partial name match."""
    user = await create_test_user(client, "org_filter@example.com")

    # Create organizations with different names
    org1_resp = await client.post(
        "/v0/organizations",
        json={"name": "Alpha Corp"},
        headers=user["headers"],
    )
    assert org1_resp.status_code == status.HTTP_201_CREATED

    org2_resp = await client.post(
        "/v0/organizations",
        json={"name": "Beta Industries"},
        headers=user["headers"],
    )
    assert org2_resp.status_code == status.HTTP_201_CREATED

    org3_resp = await client.post(
        "/v0/organizations",
        json={"name": "Alpha Technologies"},
        headers=user["headers"],
    )
    assert org3_resp.status_code == status.HTTP_201_CREATED

    # Filter by "Alpha" - should match 2 orgs
    resp = await client.get(
        "/v0/admin/organizations?name=Alpha",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    alpha_orgs = [o for o in data["organizations"] if "Alpha" in o["name"]]
    assert len(alpha_orgs) >= 2

    # Filter by "Beta" - should match 1 org
    resp2 = await client.get(
        "/v0/admin/organizations?name=Beta",
        headers=ADMIN_HEADERS,
    )
    assert resp2.status_code == status.HTTP_200_OK
    data2 = resp2.json()
    beta_orgs = [o for o in data2["organizations"] if "Beta" in o["name"]]
    assert len(beta_orgs) >= 1

    # Filter by non-existent name
    resp3 = await client.get(
        "/v0/admin/organizations?name=NonExistentOrgXYZ123",
        headers=ADMIN_HEADERS,
    )
    assert resp3.status_code == status.HTTP_200_OK
    data3 = resp3.json()
    assert len(data3["organizations"]) == 0


@pytest.mark.anyio
async def test_admin_list_organizations_case_insensitive_filter(client: AsyncClient):
    """Test that name filter is case-insensitive."""
    user = await create_test_user(client, "org_case@example.com")

    # Create an organization with mixed case name
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "CamelCaseOrg"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Filter with lowercase
    resp = await client.get(
        "/v0/admin/organizations?name=camelcase",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    matching = [o for o in data["organizations"] if o["id"] == org_id]
    assert len(matching) == 1

    # Filter with uppercase
    resp2 = await client.get(
        "/v0/admin/organizations?name=CAMELCASE",
        headers=ADMIN_HEADERS,
    )
    assert resp2.status_code == status.HTTP_200_OK
    data2 = resp2.json()
    matching2 = [o for o in data2["organizations"] if o["id"] == org_id]
    assert len(matching2) == 1


@pytest.mark.anyio
async def test_admin_list_organizations_requires_admin(client: AsyncClient):
    """Test that the endpoint requires admin authentication."""
    # Create a regular user
    user = await create_test_user(client, "regular_user@example.com")

    # Try to access admin endpoint with regular user headers
    resp = await client.get(
        "/v0/admin/organizations",
        headers=user["headers"],
    )
    # Should be forbidden or unauthorized
    assert resp.status_code in [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN]


@pytest.mark.anyio
async def test_admin_list_organizations_response_structure(client: AsyncClient):
    """Test that response has correct structure and field types."""
    user = await create_test_user(client, "org_structure@example.com")

    # Create an organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Structure Test Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]

    # Get organizations
    resp = await client.get("/v0/admin/organizations", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()

    # Verify response structure
    assert "organizations" in data
    assert "limit" in data
    assert "offset" in data
    assert isinstance(data["organizations"], list)
    assert isinstance(data["limit"], int)
    assert isinstance(data["offset"], int)

    # Find our org and verify field types
    our_org = next((o for o in data["organizations"] if o["id"] == org_id), None)
    assert our_org is not None
    assert isinstance(our_org["id"], int)
    assert isinstance(our_org["name"], str)
    assert isinstance(our_org["owner_id"], str)
    assert isinstance(our_org["member_count"], int)
    # billing_user_id can be None or string
    assert our_org["billing_user_id"] is None or isinstance(
        our_org["billing_user_id"],
        str,
    )
    # created_at can be None or string (ISO format)
    assert our_org["created_at"] is None or isinstance(our_org["created_at"], str)
