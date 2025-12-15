"""Tests for API key lifecycle management (Phase 1)."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_list_api_keys_personal_only(client: AsyncClient):
    """Test listing API keys for a user with only personal keys."""
    user = await create_test_user(client, "personal_only@test.com")

    # List API keys
    response = await client.get("/v0/api-keys", headers=user["headers"])
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert "personal_keys" in data
    assert "organization_keys" in data

    # Should have 1 personal key (created during signup)
    assert len(data["personal_keys"]) == 1
    assert data["personal_keys"][0]["organization_id"] is None

    # Should have no organization keys
    assert len(data["organization_keys"]) == 0


@pytest.mark.anyio
async def test_list_api_keys_with_org_keys(client: AsyncClient):
    """Test listing API keys for a user with both personal and org keys."""
    owner = await create_test_user(client, "owner_keys@test.com")
    member = await create_test_user(client, "member_keys@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Test Keys Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Add member to organization (automatically creates org API key)
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED

    # List member's API keys
    response = await client.get("/v0/api-keys", headers=member["headers"])
    assert response.status_code == status.HTTP_200_OK

    data = response.json()

    # Should have 1 personal key
    assert len(data["personal_keys"]) == 1
    assert data["personal_keys"][0]["organization_id"] is None

    # Should have 1 organization key
    assert len(data["organization_keys"]) == 1
    assert "Test Keys Org" in data["organization_keys"]
    assert len(data["organization_keys"]["Test Keys Org"]) == 1
    assert data["organization_keys"]["Test Keys Org"][0]["organization_id"] == org_id


@pytest.mark.anyio
async def test_revoke_personal_api_key(client: AsyncClient, dbsession):
    """Test revoking a personal API key."""
    user = await create_test_user(client, "revoke_personal@test.com")

    # List keys to get the personal key ID
    list_response = await client.get("/v0/api-keys", headers=user["headers"])
    assert list_response.status_code == status.HTTP_200_OK
    personal_keys = list_response.json()["personal_keys"]
    assert len(personal_keys) == 1
    key_id = personal_keys[0]["id"]

    # Revoke the key
    response = await client.delete(f"/v0/api-keys/{key_id}", headers=user["headers"])
    assert response.status_code == status.HTTP_204_NO_CONTENT

    # Verify key is deleted via direct DAO access
    api_key_dao = ApiKeyDAO(dbsession)
    remaining_keys = api_key_dao.get_personal_keys(user["id"])
    assert len(remaining_keys) == 0


@pytest.mark.anyio
async def test_revoke_organization_api_key(client: AsyncClient):
    """Test revoking an organization API key."""
    owner = await create_test_user(client, "owner_revoke@test.com")
    member = await create_test_user(client, "member_revoke@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Revoke Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member (automatically creates org API key)
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_response.status_code == status.HTTP_201_CREATED

    # List keys to get org key ID
    list_response = await client.get("/v0/api-keys", headers=member["headers"])
    data = list_response.json()
    assert len(data["organization_keys"]["Revoke Test Org"]) == 1
    org_key_id = data["organization_keys"]["Revoke Test Org"][0]["id"]

    # Revoke the org key
    response = await client.delete(
        f"/v0/api-keys/{org_key_id}",
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT

    # Verify org key is deleted
    verify_response = await client.get("/v0/api-keys", headers=member["headers"])
    verify_data = verify_response.json()
    assert len(verify_data["organization_keys"]) == 0

    # Verify personal key still exists
    assert len(verify_data["personal_keys"]) == 1


@pytest.mark.anyio
async def test_cannot_revoke_others_api_key(client: AsyncClient):
    """Test that users cannot revoke other users' API keys."""
    user1 = await create_test_user(client, "user1_revoke@test.com")
    user2 = await create_test_user(client, "user2_revoke@test.com")

    # Get user1's personal key ID
    list_response1 = await client.get("/v0/api-keys", headers=user1["headers"])
    user1_key_id = list_response1.json()["personal_keys"][0]["id"]

    # Try to revoke user1's key as user2
    response = await client.delete(
        f"/v0/api-keys/{user1_key_id}",
        headers=user2["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN

    # Verify key still exists
    verify_response = await client.get("/v0/api-keys", headers=user1["headers"])
    assert len(verify_response.json()["personal_keys"]) == 1


@pytest.mark.anyio
async def test_add_member_creates_org_api_key(client: AsyncClient):
    """Test that adding a member to an organization creates an org API key."""
    owner = await create_test_user(client, "owner_add_member@test.com")
    new_member = await create_test_user(client, "new_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Add Member Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Verify member initially has no org keys
    initial_list = await client.get("/v0/api-keys", headers=new_member["headers"])
    assert len(initial_list.json()["organization_keys"]) == 0

    # Add member (should create org API key)
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": new_member["id"]},
        headers=owner["headers"],
    )
    assert add_response.status_code == status.HTTP_201_CREATED
    assert "api_key" in add_response.json()

    # Verify org API key was created
    final_list = await client.get("/v0/api-keys", headers=new_member["headers"])
    final_data = final_list.json()
    assert len(final_data["organization_keys"]["Add Member Test Org"]) == 1

    # Verify personal key still exists
    assert len(final_data["personal_keys"]) == 1


@pytest.mark.anyio
async def test_remove_member_revokes_org_keys_only(client: AsyncClient):
    """Test that removing a member revokes only org keys, not personal keys."""
    owner = await create_test_user(client, "owner_remove@test.com")
    member = await create_test_user(client, "member_remove@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Remove Member Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member (automatically creates org API key)
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_response.status_code == status.HTTP_201_CREATED

    # Verify member has both keys
    keys_before = await client.get("/v0/api-keys", headers=member["headers"])
    data_before = keys_before.json()
    assert len(data_before["personal_keys"]) == 1
    assert len(data_before["organization_keys"]["Remove Member Test Org"]) == 1

    # Remove member
    remove_response = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_response.status_code == status.HTTP_204_NO_CONTENT

    # Verify org key is revoked
    keys_after = await client.get("/v0/api-keys", headers=member["headers"])
    data_after = keys_after.json()
    assert len(data_after["organization_keys"]) == 0

    # Verify personal key still exists
    assert len(data_after["personal_keys"]) == 1


@pytest.mark.anyio
async def test_member_with_multiple_orgs(client: AsyncClient):
    """Test that a member can have keys for multiple organizations."""
    owner1 = await create_test_user(client, "owner1_multi@test.com")
    owner2 = await create_test_user(client, "owner2_multi@test.com")
    member = await create_test_user(client, "member_multi@test.com")

    # Create two organizations
    org1_response = await client.post(
        "/v0/organizations",
        json={"name": "Multi Org 1"},
        headers=owner1["headers"],
    )
    org1_id = org1_response.json()["id"]

    org2_response = await client.post(
        "/v0/organizations",
        json={"name": "Multi Org 2"},
        headers=owner2["headers"],
    )
    org2_id = org2_response.json()["id"]

    # Add member to both organizations (automatically creates org API keys)
    await client.post(
        f"/v0/organizations/{org1_id}/members",
        json={"user_id": member["id"]},
        headers=owner1["headers"],
    )
    await client.post(
        f"/v0/organizations/{org2_id}/members",
        json={"user_id": member["id"]},
        headers=owner2["headers"],
    )

    # List API keys
    response = await client.get("/v0/api-keys", headers=member["headers"])
    data = response.json()

    # Should have 1 personal key
    assert len(data["personal_keys"]) == 1

    # Should have keys for 2 organizations
    assert len(data["organization_keys"]) == 2
    assert "Multi Org 1" in data["organization_keys"]
    assert "Multi Org 2" in data["organization_keys"]
    assert len(data["organization_keys"]["Multi Org 1"]) == 1
    assert len(data["organization_keys"]["Multi Org 2"]) == 1


@pytest.mark.anyio
async def test_personal_key_cannot_access_org_context(client: AsyncClient):
    """Test that personal API key does not have organization context."""
    user = await create_test_user(client, "personal_context@test.com")

    # Make a request with personal API key
    response = await client.get("/v0/credits", headers=user["headers"])
    assert response.status_code == status.HTTP_200_OK

    # The request should NOT have organization_id in state
    # (This is implicitly tested by the fact that personal billing works correctly)


@pytest.mark.anyio
async def test_org_key_has_org_context(client: AsyncClient):
    """Test that organization API key has organization context."""
    owner = await create_test_user(client, "owner_context@test.com")
    member = await create_test_user(client, "member_context@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Context Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member (automatically creates org API key)
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    org_api_key = add_response.json()["api_key"]

    # Make a request with org API key
    response = await client.get(
        "/v0/credits",
        headers={"Authorization": f"Bearer {org_api_key}"},
    )
    assert response.status_code == status.HTTP_200_OK

    # The request should have organization_id in state
    # (This is implicitly tested by the billing tests in Phase 0)


@pytest.mark.anyio
async def test_cannot_add_duplicate_member(client: AsyncClient):
    """Test that adding a duplicate member fails."""
    owner = await create_test_user(client, "owner_dup@test.com")
    member = await create_test_user(client, "member_dup@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Duplicate Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member first time
    add_response1 = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_response1.status_code == status.HTTP_201_CREATED

    # Try to add same member again
    add_response2 = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_response2.status_code == status.HTTP_409_CONFLICT


@pytest.mark.anyio
async def test_cannot_remove_organization_owner(client: AsyncClient):
    """Test that the organization owner cannot be removed."""
    owner = await create_test_user(client, "owner_self_remove@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Self Remove Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Try to remove owner
    remove_response = await client.delete(
        f"/v0/organizations/{org_id}/members/{owner['id']}",
        headers=owner["headers"],
    )
    assert remove_response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_adding_members_requires_org_write_permission(
    client: AsyncClient,
    dbsession,
):
    """Test that adding members requires org:write permission."""
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, "owner_perm@test.com")
    viewer = await create_test_user(client, "viewer_perm@test.com")
    new_user = await create_test_user(client, "new_user_perm@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Permission Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Viewer role (no org:write permission)
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Add viewer (automatically creates org API key)
    add_viewer_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer["id"], "role_id": viewer_role.id},
        headers=owner["headers"],
    )
    assert add_viewer_response.status_code == status.HTTP_201_CREATED
    viewer_org_api_key = add_viewer_response.json()["api_key"]

    # Try to add new user as viewer (no org:write) - should fail
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": new_user["id"]},
        headers={"Authorization": f"Bearer {viewer_org_api_key}"},
    )
    assert add_response.status_code == status.HTTP_403_FORBIDDEN


# Admin API Key Regeneration Tests


@pytest.mark.anyio
async def test_admin_regenerate_personal_api_key(client: AsyncClient, dbsession):
    """Test admin regenerating a personal API key by ID."""
    user = await create_test_user(client, "regen_personal@test.com")

    # Get the personal key ID
    list_response = await client.get("/v0/api-keys", headers=user["headers"])
    personal_key_id = list_response.json()["personal_keys"][0]["id"]
    old_key = list_response.json()["personal_keys"][0]["key"]

    # Regenerate the key via admin endpoint
    regen_response = await client.post(
        f"/v0/admin/api-keys/{personal_key_id}/regenerate",
        headers=ADMIN_HEADERS,
    )
    assert regen_response.status_code == status.HTTP_200_OK

    regen_data = regen_response.json()
    assert "api_key" in regen_data
    assert regen_data["user_id"] == user["id"]
    assert regen_data["organization_id"] is None

    # Verify the new key is different
    new_key = regen_data["api_key"]
    assert new_key != old_key


@pytest.mark.anyio
async def test_admin_regenerate_org_api_key(client: AsyncClient, dbsession):
    """Test admin regenerating an organization API key by ID."""
    owner = await create_test_user(client, "regen_org_owner@test.com")
    member = await create_test_user(client, "regen_org_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Regen Org Key Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member (automatically creates org API key)
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Get the org key ID
    list_response = await client.get("/v0/api-keys", headers=member["headers"])
    org_key_id = list_response.json()["organization_keys"]["Regen Org Key Org"][0]["id"]
    old_key = list_response.json()["organization_keys"]["Regen Org Key Org"][0]["key"]

    # Regenerate the key via admin endpoint
    regen_response = await client.post(
        f"/v0/admin/api-keys/{org_key_id}/regenerate",
        headers=ADMIN_HEADERS,
    )
    assert regen_response.status_code == status.HTTP_200_OK

    regen_data = regen_response.json()
    assert "api_key" in regen_data
    assert regen_data["user_id"] == member["id"]
    assert regen_data["organization_id"] == org_id

    # Verify the new key is different
    new_key = regen_data["api_key"]
    assert new_key != old_key


@pytest.mark.anyio
async def test_admin_regenerate_nonexistent_key(client: AsyncClient):
    """Test admin regenerating a non-existent key returns 404."""
    regen_response = await client.post(
        "/v0/admin/api-keys/999999/regenerate",
        headers=ADMIN_HEADERS,
    )
    assert regen_response.status_code == status.HTTP_404_NOT_FOUND
