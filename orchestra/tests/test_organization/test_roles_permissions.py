"""Tests for Phase 2: RBAC Foundation - Roles and Permissions."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.permission_dao import PermissionDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.tests.utils import create_test_user


@pytest.mark.anyio
async def test_list_permissions(client: AsyncClient):
    """Test listing all permissions."""
    user = await create_test_user(client, "perm_list@test.com")

    response = await client.get("/v0/permissions", headers=user["headers"])
    assert response.status_code == status.HTTP_200_OK

    permissions = response.json()
    # Should have 11 permissions (4 resources: project, org, billing, assistant)
    # project:read, project:write, project:delete
    # org:read, org:write, org:delete
    # billing:read, billing:write
    # assistant:read, assistant:write, assistant:delete
    assert len(permissions) == 11
    assert any(p["name"] == "project:read" for p in permissions)
    assert any(p["name"] == "project:write" for p in permissions)
    assert any(p["name"] == "project:delete" for p in permissions)
    assert any(p["name"] == "org:read" for p in permissions)
    assert any(p["name"] == "org:write" for p in permissions)
    assert any(p["name"] == "org:delete" for p in permissions)


@pytest.mark.anyio
async def test_list_permissions_by_resource_type(client: AsyncClient):
    """Test listing permissions filtered by resource type."""
    user = await create_test_user(client, "perm_filter@test.com")

    response = await client.get(
        "/v0/permissions",
        params={"resource_type": "project"},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    permissions = response.json()
    # Should have 3 project permissions (read, write, delete)
    assert len(permissions) == 3
    # All returned permissions should be for 'project' resource
    assert all(p["resource_type"] == "project" for p in permissions)
    perm_names = [p["name"] for p in permissions]
    assert "project:read" in perm_names
    assert "project:write" in perm_names
    assert "project:delete" in perm_names


@pytest.mark.anyio
async def test_list_organization_roles(client: AsyncClient):
    """Test listing roles for an organization (system roles)."""
    owner = await create_test_user(client, "role_list_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Role List Test Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # List roles
    response = await client.get(
        f"/v0/organizations/{org_id}/roles",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    roles = response.json()
    # Should have 4 system roles: Owner, Admin, Member, Viewer
    system_role_names = [r["name"] for r in roles if r["is_system_role"]]
    assert "Owner" in system_role_names
    assert "Admin" in system_role_names
    assert "Member" in system_role_names
    assert "Viewer" in system_role_names


@pytest.mark.anyio
async def test_create_custom_role(client: AsyncClient, dbsession):
    """Test creating a custom role."""
    owner = await create_test_user(client, "custom_role_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Custom Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get some permission IDs
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")
    org_read_id = next(p["id"] for p in permissions if p["name"] == "org:read")

    # Create custom role
    role_data = {
        "name": "Project Reader",
        "description": "Can read projects and organizations",
        "permission_ids": [project_read_id, org_read_id],
    }

    response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json=role_data,
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED

    role = response.json()
    assert role["name"] == "Project Reader"
    assert role["description"] == "Can read projects and organizations"
    assert role["organization_id"] == org_id
    assert role["is_system_role"] is False
    assert len(role["permissions"]) == 2


@pytest.mark.anyio
async def test_cannot_create_duplicate_role(client: AsyncClient):
    """Test that creating a duplicate role name fails."""
    owner = await create_test_user(client, "dup_role_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Duplicate Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create first custom role
    role_data = {
        "name": "Custom Role",
        "description": "First role",
        "permission_ids": [],
    }
    response1 = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json=role_data,
        headers=owner["headers"],
    )
    assert response1.status_code == status.HTTP_201_CREATED

    # Try to create duplicate
    response2 = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json=role_data,
        headers=owner["headers"],
    )
    assert response2.status_code == status.HTTP_409_CONFLICT


@pytest.mark.anyio
async def test_non_owner_cannot_create_role(client: AsyncClient):
    """Test that non-owners cannot create custom roles."""
    owner = await create_test_user(client, "role_create_owner@test.com")
    member = await create_test_user(client, "role_create_member@test.com")

    # Create organization and add member
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Role Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Try to create role as member
    role_data = {
        "name": "Unauthorized Role",
        "description": "test",
        "permission_ids": [],
    }
    response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json=role_data,
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_get_role_details(client: AsyncClient):
    """Test getting details of a specific role."""
    owner = await create_test_user(client, "role_get_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Get Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create custom role
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_create_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Test Reader",
            "description": "Test role",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    role_id = role_create_response.json()["id"]

    # Get role details
    response = await client.get(
        f"/v0/organizations/{org_id}/roles/{role_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    role = response.json()
    assert role["id"] == role_id
    assert role["name"] == "Test Reader"
    assert len(role["permissions"]) == 1
    assert role["permissions"][0]["name"] == "project:read"


@pytest.mark.anyio
async def test_get_system_role(client: AsyncClient, dbsession):
    """Test getting details of a system role."""
    owner = await create_test_user(client, "system_role_get@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "System Role Get Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get system roles to find Owner role ID
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    owner_role_id = owner_role.id

    # Get Owner role details
    response = await client.get(
        f"/v0/organizations/{org_id}/roles/{owner_role_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    role = response.json()
    assert role["name"] == "Owner"
    assert role["is_system_role"] is True
    assert len(role["permissions"]) == 11  # Owner should have all 11 permissions


@pytest.mark.anyio
async def test_update_custom_role(client: AsyncClient):
    """Test updating a custom role."""
    owner = await create_test_user(client, "role_update_owner@test.com")

    # Create organization and custom role
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Update Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    role_create_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Original Name",
            "description": "Original desc",
            "permission_ids": [],
        },
        headers=owner["headers"],
    )
    role_id = role_create_response.json()["id"]

    # Update role
    update_data = {"name": "Updated Name", "description": "Updated description"}
    response = await client.patch(
        f"/v0/organizations/{org_id}/roles/{role_id}",
        json=update_data,
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    updated_role = response.json()
    assert updated_role["name"] == "Updated Name"
    assert updated_role["description"] == "Updated description"


@pytest.mark.anyio
async def test_cannot_update_system_role(client: AsyncClient, dbsession):
    """Test that system roles cannot be updated."""
    owner = await create_test_user(client, "system_update@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "System Update Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Owner role ID
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    owner_role_id = owner_role.id

    # Try to update system role
    response = await client.patch(
        f"/v0/organizations/{org_id}/roles/{owner_role_id}",
        json={"name": "Hacked Owner", "description": "Should not work"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_delete_custom_role(client: AsyncClient):
    """Test deleting a custom role."""
    owner = await create_test_user(client, "role_delete_owner@test.com")

    # Create organization and custom role
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Delete Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    role_create_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={"name": "To Delete", "description": "test", "permission_ids": []},
        headers=owner["headers"],
    )
    role_id = role_create_response.json()["id"]

    # Delete role
    response = await client.delete(
        f"/v0/organizations/{org_id}/roles/{role_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT

    # Verify role is deleted
    get_response = await client.get(
        f"/v0/organizations/{org_id}/roles/{role_id}",
        headers=owner["headers"],
    )
    assert get_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_cannot_delete_system_role(client: AsyncClient, dbsession):
    """Test that system roles cannot be deleted."""
    owner = await create_test_user(client, "system_delete@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "System Delete Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Owner role ID
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    owner_role_id = owner_role.id

    # Try to delete system role
    response = await client.delete(
        f"/v0/organizations/{org_id}/roles/{owner_role_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_add_permissions_to_role(client: AsyncClient):
    """Test adding permissions to a custom role."""
    owner = await create_test_user(client, "role_add_perm_owner@test.com")

    # Create organization and custom role
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Add Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    role_create_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={"name": "Growing Role", "description": "test", "permission_ids": []},
        headers=owner["headers"],
    )
    role_id = role_create_response.json()["id"]

    # Get permission IDs
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")
    project_write_id = next(
        p["id"] for p in permissions if p["name"] == "project:write"
    )

    # Add permissions
    response = await client.post(
        f"/v0/organizations/{org_id}/roles/{role_id}/permissions",
        json={"permission_ids": [project_read_id, project_write_id]},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    updated_role = response.json()
    assert len(updated_role["permissions"]) == 2
    perm_names = [p["name"] for p in updated_role["permissions"]]
    assert "project:read" in perm_names
    assert "project:write" in perm_names


@pytest.mark.anyio
async def test_remove_permissions_from_role(client: AsyncClient):
    """Test removing permissions from a custom role."""
    owner = await create_test_user(client, "role_remove_perm_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Remove Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get permission IDs
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")
    project_write_id = next(
        p["id"] for p in permissions if p["name"] == "project:write"
    )

    # Create role with permissions
    role_create_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Shrinking Role",
            "description": "test",
            "permission_ids": [project_read_id, project_write_id],
        },
        headers=owner["headers"],
    )
    role_id = role_create_response.json()["id"]

    # Remove one permission
    response = await client.delete(
        f"/v0/organizations/{org_id}/roles/{role_id}/permissions/{project_write_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    updated_role = response.json()
    assert len(updated_role["permissions"]) == 1
    assert updated_role["permissions"][0]["name"] == "project:read"


@pytest.mark.anyio
async def test_cannot_modify_system_role_permissions(client: AsyncClient, dbsession):
    """Test that system role permissions cannot be modified."""
    owner = await create_test_user(client, "system_mod_perm@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "System Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Member role ID
    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    member_role_id = member_role.id

    # Get a permission ID
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    org_delete_id = next(p["id"] for p in permissions if p["name"] == "org:delete")

    # Try to add permission to system role
    response = await client.post(
        f"/v0/organizations/{org_id}/roles/{member_role_id}/permissions",
        json={"permission_ids": [org_delete_id]},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_system_role_permissions(client: AsyncClient, dbsession):
    """Test that system roles have correct permissions."""
    owner = await create_test_user(client, "system_perm_check@test.com")

    role_dao = RoleDAO(dbsession)
    permission_dao = PermissionDAO(dbsession)

    # Check Owner role has all permissions (8 total: project×3 + org×3 + billing×2)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    owner_perms = role_dao.get_role_permissions(owner_role.id)
    all_perms = permission_dao.list_all()
    assert len(owner_perms) == len(all_perms) == 11

    # Check Admin role does not have org:delete (should have 10 permissions)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    admin_perms = role_dao.get_role_permissions(admin_role.id)
    assert len(admin_perms) == 10
    assert not role_dao.has_permission(admin_role.id, "org:delete")
    assert role_dao.has_permission(admin_role.id, "org:read")
    assert role_dao.has_permission(admin_role.id, "org:write")
    assert role_dao.has_permission(admin_role.id, "project:delete")

    # Check Member role has project read/write + org read + billing read + assistant read/write (6 permissions)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    member_perms = role_dao.get_role_permissions(member_role.id)
    assert len(member_perms) == 6
    assert role_dao.has_permission(member_role.id, "project:read")
    assert role_dao.has_permission(member_role.id, "project:write")
    assert role_dao.has_permission(member_role.id, "org:read")
    assert not role_dao.has_permission(member_role.id, "org:write")
    assert not role_dao.has_permission(member_role.id, "project:delete")
    assert not role_dao.has_permission(member_role.id, "org:delete")

    # Check Viewer role has only read permissions (4 permissions: project, org, billing, assistant read)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    viewer_perms = role_dao.get_role_permissions(viewer_role.id)
    assert len(viewer_perms) == 4
    assert all(p.action == "read" for p in viewer_perms)


@pytest.mark.anyio
async def test_custom_role_across_organizations(client: AsyncClient):
    """Test that custom roles are organization-specific."""
    owner1 = await create_test_user(client, "org1_owner@test.com")
    owner2 = await create_test_user(client, "org2_owner@test.com")

    # Create two organizations
    org1_response = await client.post(
        "/v0/organizations",
        json={"name": "Org 1"},
        headers=owner1["headers"],
    )
    org1_id = org1_response.json()["id"]

    org2_response = await client.post(
        "/v0/organizations",
        json={"name": "Org 2"},
        headers=owner2["headers"],
    )
    org2_id = org2_response.json()["id"]

    # Create custom role in Org 1
    role1_response = await client.post(
        f"/v0/organizations/{org1_id}/roles",
        json={"name": "Custom Role", "description": "Org 1 role", "permission_ids": []},
        headers=owner1["headers"],
    )
    role1_id = role1_response.json()["id"]

    # Create custom role with same name in Org 2 (should succeed)
    role2_response = await client.post(
        f"/v0/organizations/{org2_id}/roles",
        json={"name": "Custom Role", "description": "Org 2 role", "permission_ids": []},
        headers=owner2["headers"],
    )
    assert role2_response.status_code == status.HTTP_201_CREATED
    role2_id = role2_response.json()["id"]

    # Roles should have different IDs
    assert role1_id != role2_id

    # Owner 2 should not be able to access Org 1's custom role
    response = await client.get(
        f"/v0/organizations/{org2_id}/roles/{role1_id}",
        headers=owner2["headers"],
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
