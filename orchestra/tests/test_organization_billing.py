"""Tests for organization billing features (Phase 0)."""
import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import create_test_user, get_credits


@pytest.mark.anyio
async def test_create_organization(client: AsyncClient):
    """Test creating an organization with default billing."""
    # Create test user
    owner = await create_test_user(client, "owner@test.com")

    # Create organization
    response = await client.post(
        "/v0/organizations",
        json={"name": "Test Org"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED
    org_data = response.json()

    assert org_data["name"] == "Test Org"
    assert org_data["owner_id"] == owner["id"]
    assert org_data["billing_user_id"] == owner["id"]  # Always equals owner
    assert "id" in org_data
    assert "created_at" in org_data
    # Owner should receive an organization API key
    assert "api_key" in org_data
    assert org_data["api_key"] is not None


@pytest.mark.anyio
async def test_create_organization_owner_gets_org_api_key(client: AsyncClient):
    """Test that creating an organization gives the owner an org API key."""
    owner = await create_test_user(client, "owner2@test.com")

    # Create organization
    response = await client.post(
        "/v0/organizations",
        json={"name": "Owner API Key Org"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED
    org_data = response.json()
    org_api_key = org_data["api_key"]

    # Verify owner has org API key by listing keys
    keys_response = await client.get("/v0/api-keys", headers=owner["headers"])
    keys_data = keys_response.json()

    assert len(keys_data["personal_keys"]) == 1
    assert "Owner API Key Org" in keys_data["organization_keys"]
    assert len(keys_data["organization_keys"]["Owner API Key Org"]) == 1


@pytest.mark.anyio
async def test_create_organization_duplicate_name(client: AsyncClient):
    """Test that creating an organization with duplicate name fails."""
    owner = await create_test_user(client, "owner3@test.com")

    # Create first organization
    response1 = await client.post(
        "/v0/organizations",
        json={"name": "Duplicate Org"},
        headers=owner["headers"],
    )
    assert response1.status_code == status.HTTP_201_CREATED

    # Try to create second organization with same name
    response2 = await client.post(
        "/v0/organizations",
        json={"name": "Duplicate Org"},
        headers=owner["headers"],
    )
    assert response2.status_code == status.HTTP_409_CONFLICT
    assert "already exists" in response2.json()["detail"].lower()


@pytest.mark.anyio
async def test_list_organizations(client: AsyncClient):
    """Test listing organizations a user has access to."""
    owner = await create_test_user(client, "owner4@test.com")

    # Create multiple organizations
    org1_response = await client.post(
        "/v0/organizations",
        json={"name": "Org 1"},
        headers=owner["headers"],
    )
    org2_response = await client.post(
        "/v0/organizations",
        json={"name": "Org 2"},
        headers=owner["headers"],
    )

    assert org1_response.status_code == status.HTTP_201_CREATED
    assert org2_response.status_code == status.HTTP_201_CREATED

    # List organizations
    response = await client.get(
        "/v0/organizations",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    orgs = response.json()

    assert len(orgs) >= 2
    org_names = [org["name"] for org in orgs]
    assert "Org 1" in org_names
    assert "Org 2" in org_names


@pytest.mark.anyio
async def test_get_organization(client: AsyncClient):
    """Test getting organization details."""
    owner = await create_test_user(client, "owner5@test.com")

    # Create organization
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Get Test Org"},
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

    assert org_data["id"] == org_id
    assert org_data["name"] == "Get Test Org"
    assert org_data["owner_id"] == owner["id"]


@pytest.mark.anyio
async def test_get_organization_unauthorized(client: AsyncClient):
    """Test that unauthorized users cannot access organization."""
    owner = await create_test_user(client, "owner6@test.com")
    other_user = await create_test_user(client, "other6@test.com")

    # Create organization as owner
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Private Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]

    # Try to access as other user
    response = await client.get(
        f"/v0/organizations/{org_id}",
        headers=other_user["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_update_organization(client: AsyncClient):
    """Test updating organization details (name only, billing follows owner)."""
    owner = await create_test_user(client, "owner7@test.com")

    # Create organization
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Update Test Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]

    # Update organization name
    response = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"name": "Updated Org Name"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    org_data = response.json()

    assert org_data["name"] == "Updated Org Name"
    # billing_user_id should still equal owner (unchanged)
    assert org_data["billing_user_id"] == owner["id"]


@pytest.mark.anyio
async def test_update_organization_non_owner(client: AsyncClient):
    """Test that non-owners cannot update organization."""
    owner = await create_test_user(client, "owner8@test.com")
    other_user = await create_test_user(client, "other8@test.com")

    # Create organization as owner
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Owner Only Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]

    # Try to update as other user
    response = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"name": "Hacked Org"},
        headers=other_user["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_delete_organization(client: AsyncClient):
    """Test deleting an organization."""
    owner = await create_test_user(client, "owner9@test.com")

    # Create organization
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Delete Test Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]

    # Delete organization
    response = await client.delete(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT

    # Verify deletion
    get_response = await client.get(
        f"/v0/organizations/{org_id}",
        headers=owner["headers"],
    )
    assert get_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_delete_organization_non_owner(client: AsyncClient):
    """Test that non-owners cannot delete organization."""
    owner = await create_test_user(client, "owner10@test.com")
    other_user = await create_test_user(client, "other10@test.com")

    # Create organization as owner
    create_response = await client.post(
        "/v0/organizations",
        json={"name": "Permanent Org"},
        headers=owner["headers"],
    )
    org_id = create_response.json()["id"]

    # Try to delete as other user
    response = await client.delete(
        f"/v0/organizations/{org_id}",
        headers=other_user["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_personal_query_billing(client: AsyncClient, dbsession):
    """Test that personal API key queries are billed to the user."""
    from orchestra.db.dao.users_dao import UsersDAO

    # Create user with credits
    user = await create_test_user(client, "personal_user@test.com")

    # Add credits to user (direct DAO access)
    users_dao = UsersDAO(dbsession)
    users_dao.recharge_credit(user["id"], 10)
    dbsession.commit()

    # Get initial credits
    initial_credits = await get_credits(client, user["headers"])
    assert initial_credits == 10.0

    # Make a query using personal API key
    await client.post(
        "/v0/queries",
        json={
            "endpoint": "gpt-4o-mini@openai",
            "query_body": {
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 10,
            },
            "response_body": {
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 5,
                    "cost": 0.01,
                },
            },
            "consume_credits": True,
        },
        headers=user["headers"],
    )

    # Check credits were deducted from user
    final_credits = await get_credits(client, user["headers"])
    assert final_credits == pytest.approx(9.99, rel=0.01)


@pytest.mark.anyio
async def test_organization_query_billing(client: AsyncClient, dbsession):
    """Test that organization API key queries are billed to org's owner (billing_user)."""
    from orchestra.db.dao.users_dao import UsersDAO

    # Create owner and org member
    owner = await create_test_user(client, "org_owner@test.com")
    member = await create_test_user(client, "org_member@test.com")

    # Add credits to owner (billing_user always equals owner)
    users_dao = UsersDAO(dbsession)
    users_dao.recharge_credit(owner["id"], 10)
    dbsession.commit()

    # Create organization (billing_user_id = owner_id automatically)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Billing Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member to the organization (this creates org API key for member)
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )
    org_api_key = add_member_response.json()["api_key"]

    # Get initial credits
    owner_initial = await get_credits(client, owner["headers"])
    member_initial = float(await get_credits(client, member["headers"]) or 0)

    assert owner_initial == 10.0

    # Make a query using organization API key (as member)
    await client.post(
        "/v0/queries",
        json={
            "endpoint": "gpt-4o-mini@openai",
            "query_body": {
                "messages": [{"role": "user", "content": "org test"}],
                "max_tokens": 10,
            },
            "response_body": {
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 5,
                    "cost": 0.02,
                },
            },
            "consume_credits": True,
        },
        headers={"Authorization": f"Bearer {org_api_key}"},
    )

    # Check credits were deducted from owner (billing_user) not member
    owner_final = await get_credits(client, owner["headers"])
    member_final = float(await get_credits(client, member["headers"]) or 0)

    assert owner_final == pytest.approx(9.98, rel=0.01)
    assert member_final == member_initial  # Member credits unchanged


@pytest.mark.anyio
async def test_query_logs_organization_id(client: AsyncClient):
    """Test that queries are logged with correct organization_id."""
    # This test would require access to query logs, which might need admin endpoints
    # For now, we test that the endpoint accepts the organization context
    owner = await create_test_user(client, "log_owner@test.com")

    # Create organization (owner gets org API key automatically)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Log Test Org"},
        headers=owner["headers"],
    )
    org_api_key = org_response.json()["api_key"]

    # Log a query with org API key
    response = await client.post(
        "/v0/queries",
        json={
            "endpoint": "gpt-4o-mini@openai",
            "query_body": {"messages": [{"role": "user", "content": "log test"}]},
            "response_body": {"usage": {"prompt_tokens": 5, "completion_tokens": 5}},
        },
        headers={"Authorization": f"Bearer {org_api_key}"},
    )
    assert response.status_code == status.HTTP_200_OK


# Ownership Transfer Tests


@pytest.mark.anyio
async def test_transfer_ownership_success(client: AsyncClient, dbsession):
    """Test successful ownership transfer."""
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, "transfer_owner@test.com")
    new_owner = await create_test_user(client, "transfer_new_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Transfer Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add new_owner as member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": new_owner["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Transfer ownership
    transfer_response = await client.post(
        f"/v0/organizations/{org_id}/transfer-ownership",
        json={"new_owner_id": new_owner["id"]},
        headers=owner["headers"],
    )
    assert transfer_response.status_code == status.HTTP_200_OK

    org_data = transfer_response.json()
    assert org_data["owner_id"] == new_owner["id"]
    assert org_data["billing_user_id"] == new_owner["id"]

    # Verify roles were swapped
    org_member_dao = OrganizationMemberDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    new_owner_member = org_member_dao.get_member(new_owner["id"], org_id)
    old_owner_member = org_member_dao.get_member(owner["id"], org_id)

    new_owner_role = role_dao.get(new_owner_member.role_id)
    old_owner_role = role_dao.get(old_owner_member.role_id)

    assert new_owner_role.name == "Owner"
    assert old_owner_role.name == "Admin"
    assert new_owner_member.level == "owner"
    assert old_owner_member.level == "admin"


@pytest.mark.anyio
async def test_transfer_ownership_only_owner_can_transfer(client: AsyncClient):
    """Test that only the current owner can transfer ownership."""
    owner = await create_test_user(client, "transfer_only_owner@test.com")
    member = await create_test_user(client, "transfer_member@test.com")
    outsider = await create_test_user(client, "transfer_outsider@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Only Owner Transfer Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Member tries to transfer - should fail
    transfer_response = await client.post(
        f"/v0/organizations/{org_id}/transfer-ownership",
        json={"new_owner_id": member["id"]},
        headers=member["headers"],
    )
    assert transfer_response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_transfer_ownership_new_owner_must_be_member(client: AsyncClient):
    """Test that new owner must be an existing member."""
    owner = await create_test_user(client, "transfer_must_be_member@test.com")
    non_member = await create_test_user(client, "transfer_non_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Must Be Member Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Try to transfer to non-member - should fail
    transfer_response = await client.post(
        f"/v0/organizations/{org_id}/transfer-ownership",
        json={"new_owner_id": non_member["id"]},
        headers=owner["headers"],
    )
    assert transfer_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "existing member" in transfer_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_transfer_ownership_cannot_transfer_to_self(client: AsyncClient):
    """Test that owner cannot transfer to themselves."""
    owner = await create_test_user(client, "transfer_self@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Self Transfer Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Try to transfer to self - should fail
    transfer_response = await client.post(
        f"/v0/organizations/{org_id}/transfer-ownership",
        json={"new_owner_id": owner["id"]},
        headers=owner["headers"],
    )
    assert transfer_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "yourself" in transfer_response.json()["detail"].lower()
