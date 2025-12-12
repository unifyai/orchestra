"""Tests for Phase 4: Member Role Management."""
import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.tests.utils import create_test_user


@pytest.mark.anyio
async def test_create_organization_assigns_owner_role(client: AsyncClient, dbsession):
    """Test that creating an organization assigns the Owner role to the creator."""
    owner = await create_test_user(client, "org_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Test Org Owner Role"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]

    # Verify org API key is returned
    assert "api_key" in org_data
    assert org_data["api_key"] is not None

    # Check that the owner has the Owner role assigned
    org_member_dao = OrganizationMemberDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    member = org_member_dao.get_member(owner["id"], org_id)
    assert member is not None
    assert member.role_id is not None

    role = role_dao.get(member.role_id)
    assert role is not None
    assert role.name == "Owner"
    assert role.is_system_role is True


@pytest.mark.anyio
async def test_add_member_with_default_role(client: AsyncClient, dbsession):
    """Test that adding a member without specifying a role assigns the Member role."""
    owner = await create_test_user(client, "member_default_owner@test.com")
    user = await create_test_user(client, "member_default_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Test Default Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member without specifying role_id
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user["id"], "level": "user"},
        headers=owner["headers"],
    )
    assert add_response.status_code == status.HTTP_201_CREATED

    # Verify member has Member role
    org_member_dao = OrganizationMemberDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    member = org_member_dao.get_member(user["id"], org_id)
    assert member is not None
    assert member.role_id is not None

    role = role_dao.get(member.role_id)
    assert role.name == "Member"


@pytest.mark.anyio
async def test_add_member_with_specific_role(client: AsyncClient, dbsession):
    """Test adding a member with a specific role."""
    owner = await create_test_user(client, "member_specific_owner@test.com")
    viewer = await create_test_user(client, "member_specific_viewer@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Test Specific Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Viewer role ID
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    assert viewer_role is not None

    # Add member with Viewer role
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={
            "user_id": viewer["id"],
            "level": "user",
            "role_id": viewer_role.id,
        },
        headers=owner["headers"],
    )
    assert add_response.status_code == status.HTTP_201_CREATED

    # Verify member has Viewer role
    org_member_dao = OrganizationMemberDAO(dbsession)
    member = org_member_dao.get_member(viewer["id"], org_id)
    assert member.role_id == viewer_role.id


@pytest.mark.anyio
async def test_list_members_shows_roles(client: AsyncClient, dbsession):
    """Test that listing members shows their role information."""
    owner = await create_test_user(client, "list_roles_owner@test.com")
    admin = await create_test_user(client, "list_roles_admin@test.com")
    viewer = await create_test_user(client, "list_roles_viewer@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "List Roles Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get role IDs
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Add members with different roles
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin["id"], "level": "admin", "role_id": admin_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer["id"], "level": "user", "role_id": viewer_role.id},
        headers=owner["headers"],
    )

    # List members
    list_response = await client.get(
        f"/v0/organizations/{org_id}/members",
        headers=owner["headers"],
    )
    assert list_response.status_code == status.HTTP_200_OK

    members = list_response.json()
    assert len(members) == 3  # owner + admin + viewer

    # Verify role information is included
    member_roles = {m["user_id"]: m["role_name"] for m in members}
    assert member_roles[owner["id"]] == "Owner"
    assert member_roles[admin["id"]] == "Admin"
    assert member_roles[viewer["id"]] == "Viewer"


@pytest.mark.anyio
async def test_update_member_role(client: AsyncClient, dbsession):
    """Test updating a member's role."""
    owner = await create_test_user(client, "update_role_owner@test.com")
    user = await create_test_user(client, "update_role_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Update Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member with Member role (default)
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Get Admin role ID
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Update member to Admin role
    update_response = await client.patch(
        f"/v0/organizations/{org_id}/members/{user['id']}/role",
        json={"role_id": admin_role.id},
        headers=owner["headers"],
    )
    assert update_response.status_code == status.HTTP_200_OK

    updated_member = update_response.json()
    assert updated_member["role_id"] == admin_role.id
    assert updated_member["role_name"] == "Admin"


@pytest.mark.anyio
async def test_cannot_update_owner_role(client: AsyncClient):
    """Test that the organization owner's role cannot be changed."""
    owner = await create_test_user(client, "protect_owner_role@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Protect Owner Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Try to update owner's role
    update_response = await client.patch(
        f"/v0/organizations/{org_id}/members/{owner['id']}/role",
        json={"role_id": 2},  # Any other role
        headers=owner["headers"],
    )
    assert update_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "owner's role" in update_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_role_update_requires_org_write_permission(
    client: AsyncClient,
    dbsession,
):
    """Test that updating member roles requires org:write permission."""
    owner = await create_test_user(client, "role_update_owner@test.com")
    admin = await create_test_user(client, "role_update_admin@test.com")
    viewer = await create_test_user(client, "role_update_viewer@test.com")
    member = await create_test_user(client, "role_update_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Role Update Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get role IDs
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    # Add admin, viewer, and member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin["id"], "level": "admin", "role_id": admin_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer["id"], "level": "user", "role_id": viewer_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Admin (has org:write) CAN update member's role
    update_response = await client.patch(
        f"/v0/organizations/{org_id}/members/{member['id']}/role",
        json={"role_id": viewer_role.id},
        headers=admin["headers"],
    )
    assert update_response.status_code == status.HTTP_200_OK

    # Viewer (no org:write) CANNOT update member's role
    update_response2 = await client.patch(
        f"/v0/organizations/{org_id}/members/{member['id']}/role",
        json={"role_id": member_role.id},
        headers=viewer["headers"],
    )
    assert update_response2.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_member_permissions_use_assigned_role(client: AsyncClient, dbsession):
    """Test that member permissions are checked using their assigned role."""
    owner = await create_test_user(client, "perm_check_owner@test.com")
    viewer_user = await create_test_user(client, "perm_check_viewer@test.com")
    admin_user = await create_test_user(client, "perm_check_admin@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Permission Check Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get role IDs
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Add members with different roles
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer_user["id"], "level": "user", "role_id": viewer_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin_user["id"], "level": "admin", "role_id": admin_role.id},
        headers=owner["headers"],
    )

    # Create org project (without explicit ResourceAccess, uses implicit membership)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    project_dao.create(
        name="Permission_Test_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(
        organization_id=org_id,
        name="Permission_Test_Project",
    )
    project = projects[0][0]

    # Viewer should have read permission (Viewer role has project:read)
    viewer_has_read = resource_access_dao.check_user_permission(
        viewer_user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert viewer_has_read is True

    # Viewer should NOT have write permission (Viewer role doesn't have project:write)
    viewer_has_write = resource_access_dao.check_user_permission(
        viewer_user["id"],
        "project",
        project.id,
        "project:write",
    )
    assert viewer_has_write is False

    # Admin should have both read and write (Admin role has both)
    admin_has_read = resource_access_dao.check_user_permission(
        admin_user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert admin_has_read is True

    admin_has_write = resource_access_dao.check_user_permission(
        admin_user["id"],
        "project",
        project.id,
        "project:write",
    )
    assert admin_has_write is True


@pytest.mark.anyio
async def test_personal_projects_unaffected_by_org_roles(
    client: AsyncClient,
    dbsession,
):
    """Test that personal projects are not affected by organization role changes."""
    user = await create_test_user(client, "personal_project_user@test.com")
    other_user = await create_test_user(client, "personal_project_other@test.com")

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    project_dao.create(
        name="Personal_Project",
        user_id=user["id"],
        organization_id=None,  # Personal project
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="Personal_Project")
    project = projects[0][0]

    # User should have full access to personal project
    user_has_read = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert user_has_read is True

    user_has_write = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:write",
    )
    assert user_has_write is True

    user_has_delete = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:delete",
    )
    assert user_has_delete is True

    # Other user should have NO access
    other_has_read = resource_access_dao.check_user_permission(
        other_user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert other_has_read is False

    # Now create an organization and add both users
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Personal Project Org"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": other_user["id"], "level": "admin"},
        headers=user["headers"],
    )

    # Personal project should STILL be inaccessible to other_user
    # even though they're in the same org
    still_no_access = resource_access_dao.check_user_permission(
        other_user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert still_no_access is False


@pytest.mark.anyio
async def test_only_system_roles_can_be_assigned(client: AsyncClient, dbsession):
    """Test that only system roles can be assigned to members."""
    owner = await create_test_user(client, "system_role_owner@test.com")
    user = await create_test_user(client, "system_role_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "System Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create a custom (non-system) role
    role_dao = RoleDAO(dbsession)
    custom_role = role_dao.create(
        name="Custom Role",
        description="Non-system custom role",
        organization_id=org_id,
        is_system_role=False,
    )
    dbsession.commit()

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Try to assign custom (non-system) role - should fail
    update_response = await client.patch(
        f"/v0/organizations/{org_id}/members/{user['id']}/role",
        json={"role_id": custom_role.id},
        headers=owner["headers"],
    )
    assert update_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "system role" in update_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_cannot_add_member_with_owner_role(client: AsyncClient, dbsession):
    """Test that adding a member with Owner role is blocked."""
    owner = await create_test_user(client, "block_owner_add@test.com")
    user = await create_test_user(client, "block_owner_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Block Owner Add Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Owner role ID
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    # Try to add member with Owner role - should fail
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={
            "user_id": user["id"],
            "level": "user",
            "role_id": owner_role.id,
        },
        headers=owner["headers"],
    )
    assert add_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "owner role" in add_response.json()["detail"].lower()
    assert "transfer" in add_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_cannot_update_member_to_owner_role(client: AsyncClient, dbsession):
    """Test that updating a member to Owner role is blocked."""
    owner = await create_test_user(client, "block_owner_update@test.com")
    user = await create_test_user(client, "block_owner_update_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Block Owner Update Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member with default role
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Get Owner role ID
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    # Try to update member to Owner role - should fail
    update_response = await client.patch(
        f"/v0/organizations/{org_id}/members/{user['id']}/role",
        json={"role_id": owner_role.id},
        headers=owner["headers"],
    )
    assert update_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "owner role" in update_response.json()["detail"].lower()
    assert "transfer" in update_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_member_list_requires_membership(client: AsyncClient):
    """Test that listing organization members requires org:read permission."""
    owner = await create_test_user(client, "list_perm_owner@test.com")
    outsider = await create_test_user(client, "list_perm_outsider@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "List Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Owner (has org:read) can list members
    list_response = await client.get(
        f"/v0/organizations/{org_id}/members",
        headers=owner["headers"],
    )
    assert list_response.status_code == status.HTTP_200_OK

    # Outsider (no org:read) cannot list members
    outsider_response = await client.get(
        f"/v0/organizations/{org_id}/members",
        headers=outsider["headers"],
    )
    assert outsider_response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_role_affects_implicit_permissions_only(client: AsyncClient, dbsession):
    """Test that member roles only affect implicit permissions, not explicit grants."""
    owner = await create_test_user(client, "implicit_explicit_owner@test.com")
    viewer_member = await create_test_user(client, "implicit_explicit_viewer@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Implicit vs Explicit Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get role IDs
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Add member with Viewer role
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={
            "user_id": viewer_member["id"],
            "level": "user",
            "role_id": viewer_role.id,
        },
        headers=owner["headers"],
    )

    # Create two org projects
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Project 1: No explicit grants (uses implicit member role)
    project_dao.create(
        name="Implicit_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects1 = project_dao.filter(organization_id=org_id, name="Implicit_Project")
    implicit_project = projects1[0][0]

    # Project 2: Explicit Admin grant
    project_dao.create(
        name="Explicit_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects2 = project_dao.filter(organization_id=org_id, name="Explicit_Project")
    explicit_project = projects2[0][0]

    # Grant explicit Admin role on second project
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=explicit_project.id,
        role_id=admin_role.id,
        grantee_type="user",
        grantee_id=viewer_member["id"],
    )
    dbsession.commit()

    # Implicit project: Viewer member has read-only (from Viewer role)
    implicit_has_read = resource_access_dao.check_user_permission(
        viewer_member["id"],
        "project",
        implicit_project.id,
        "project:read",
    )
    assert implicit_has_read is True

    implicit_has_write = resource_access_dao.check_user_permission(
        viewer_member["id"],
        "project",
        implicit_project.id,
        "project:write",
    )
    assert implicit_has_write is False

    # Explicit project: Member has write (from explicit Admin grant)
    explicit_has_read = resource_access_dao.check_user_permission(
        viewer_member["id"],
        "project",
        explicit_project.id,
        "project:read",
    )
    assert explicit_has_read is True

    explicit_has_write = resource_access_dao.check_user_permission(
        viewer_member["id"],
        "project",
        explicit_project.id,
        "project:write",
    )
    assert explicit_has_write is True


@pytest.mark.anyio
async def test_check_org_member_permission(client: AsyncClient, dbsession):
    """Test that check_org_member_permission() directly uses org member role.

    This tests the new method that checks org-level permissions based on
    OrganizationMember.role_id directly, without using ResourceAccess.
    """
    owner = await create_test_user(client, "org_member_perm_owner@test.com")
    admin_user = await create_test_user(client, "org_member_perm_admin@test.com")
    viewer_user = await create_test_user(client, "org_member_perm_viewer@test.com")
    outsider = await create_test_user(client, "org_member_perm_outsider@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Org Member Permission Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get role IDs
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Add members with different roles
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin_user["id"], "level": "admin", "role_id": admin_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer_user["id"], "level": "user", "role_id": viewer_role.id},
        headers=owner["headers"],
    )

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Owner should have org:write, org:read, org:delete (via Owner role)
    assert resource_access_dao.check_org_member_permission(
        owner["id"],
        org_id,
        "org:read",
    )
    assert resource_access_dao.check_org_member_permission(
        owner["id"],
        org_id,
        "org:write",
    )
    assert resource_access_dao.check_org_member_permission(
        owner["id"],
        org_id,
        "org:delete",
    )

    # Admin should have org:write, org:read but NOT org:delete
    assert resource_access_dao.check_org_member_permission(
        admin_user["id"],
        org_id,
        "org:read",
    )
    assert resource_access_dao.check_org_member_permission(
        admin_user["id"],
        org_id,
        "org:write",
    )
    assert not resource_access_dao.check_org_member_permission(
        admin_user["id"],
        org_id,
        "org:delete",
    )

    # Viewer should have org:read but NOT org:write or org:delete
    assert resource_access_dao.check_org_member_permission(
        viewer_user["id"],
        org_id,
        "org:read",
    )
    assert not resource_access_dao.check_org_member_permission(
        viewer_user["id"],
        org_id,
        "org:write",
    )
    assert not resource_access_dao.check_org_member_permission(
        viewer_user["id"],
        org_id,
        "org:delete",
    )

    # Outsider (not an org member) should have no permissions
    assert not resource_access_dao.check_org_member_permission(
        outsider["id"],
        org_id,
        "org:read",
    )
    assert not resource_access_dao.check_org_member_permission(
        outsider["id"],
        org_id,
        "org:write",
    )
    assert not resource_access_dao.check_org_member_permission(
        outsider["id"],
        org_id,
        "org:delete",
    )


@pytest.mark.anyio
async def test_org_member_permission_for_team_operations(
    client: AsyncClient,
    dbsession,
):
    """Test that org member permission is used for team management operations.

    Team create/update/delete operations should check org:write permission
    via the org member role, not via ResourceAccess.
    """
    owner = await create_test_user(client, "team_ops_owner@test.com")
    admin_user = await create_test_user(client, "team_ops_admin@test.com")
    viewer_user = await create_test_user(client, "team_ops_viewer@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team Ops Permission Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get role IDs
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Add admin and viewer
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin_user["id"], "level": "admin", "role_id": admin_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer_user["id"], "level": "user", "role_id": viewer_role.id},
        headers=owner["headers"],
    )

    # Admin (has org:write) CAN create a team
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Admin Created Team"},
        headers=admin_user["headers"],
    )
    assert team_response.status_code == status.HTTP_201_CREATED

    # Viewer (no org:write) CANNOT create a team
    viewer_team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Viewer Created Team"},
        headers=viewer_user["headers"],
    )
    assert viewer_team_response.status_code == status.HTTP_403_FORBIDDEN
