"""Tests for Custom Roles with ResourceAccess (explicit project grants)."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.tests.utils import create_test_user


@pytest.mark.anyio
async def test_grant_project_access_with_custom_role(client: AsyncClient, dbsession):
    """Test granting explicit project access using a custom role."""
    owner = await create_test_user(client, "custom_grant_owner@test.com")
    member = await create_test_user(client, "custom_grant_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Custom Role Grant Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Add member to organization
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create a custom role with only project:read permission
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Project Reader Only",
            "description": "Can only read specific projects",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    assert role_response.status_code == status.HTTP_201_CREATED
    custom_role_id = role_response.json()["id"]

    # Create an org project using DAO (to share same session)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,  # Org projects don't have user_id
        name="Restricted_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="Restricted_Project")
    project = projects[0][0]
    project_id = project.id

    # Grant explicit access to member with custom role
    resource_access_dao = ResourceAccessDAO(dbsession)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=custom_role_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Verify explicit grant exists
    access_entries = resource_access_dao.get_resource_access("project", project_id)
    assert len(access_entries) == 1
    assert access_entries[0].role_id == custom_role_id
    assert access_entries[0].grantee_id == member["id"]


@pytest.mark.anyio
async def test_custom_role_grants_read_permission_via_resource_access(
    client: AsyncClient,
    dbsession,
):
    """Test that custom role with project:read allows reading the project."""
    owner = await create_test_user(client, "read_perm_owner@test.com")
    member = await create_test_user(client, "read_perm_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Read Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create custom role with project:read
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Reader Role",
            "description": "Read only",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    custom_role_id = role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="ReadTest_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="ReadTest_Project")
    project = projects[0][0]
    project_id = project.id

    # Grant explicit access
    resource_access_dao = ResourceAccessDAO(dbsession)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=custom_role_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Verify permission check works
    has_read = resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    )
    assert has_read is True

    # Verify write permission is NOT granted
    has_write = resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    )
    assert has_write is False


@pytest.mark.anyio
async def test_custom_role_denies_unpermitted_actions(client: AsyncClient, dbsession):
    """Test that custom role without write permission denies write actions."""
    owner = await create_test_user(client, "deny_write_owner@test.com")
    member = await create_test_user(client, "deny_write_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Deny Write Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create custom role with ONLY project:read (no write)
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Read Only Role",
            "description": "No write access",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    custom_role_id = role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="NoWrite_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="NoWrite_Project")
    project = projects[0][0]
    project_id = project.id

    # Grant explicit access with read-only role
    resource_access_dao = ResourceAccessDAO(dbsession)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=custom_role_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Verify: has read, no write, no delete
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    )
    assert not resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    )
    assert not resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:delete",
    )


@pytest.mark.anyio
async def test_explicit_grant_overrides_implicit_membership(
    client: AsyncClient,
    dbsession,
):
    """Test that explicit ResourceAccess grants take precedence over implicit membership."""
    owner = await create_test_user(client, "override_owner@test.com")
    member = await create_test_user(client, "override_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Override Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member with default Member role (has project:read, project:write)
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create custom role with ONLY project:read (no write)
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Restricted Reader",
            "description": "Read only for specific project",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    custom_role_id = role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="Override_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="Override_Project")
    project = projects[0][0]
    project_id = project.id

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Before explicit grant: member should have write via implicit membership
    # (Member role has project:write)
    has_write_before = resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    )
    assert has_write_before is True, "Member should have write via implicit membership"

    # Grant explicit access with read-only custom role
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=custom_role_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Clear permission cache to ensure fresh check
    ResourceAccessDAO.clear_permission_cache()

    # After explicit grant: member should only have read (explicit overrides implicit)
    has_read_after = resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    )
    has_write_after = resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    )

    assert has_read_after is True, "Should have read via explicit grant"
    assert (
        has_write_after is False
    ), "Explicit grant should override implicit membership"


@pytest.mark.anyio
async def test_revoke_custom_role_access(client: AsyncClient, dbsession):
    """Test revoking explicit resource access with custom role."""
    owner = await create_test_user(client, "revoke_owner@test.com")
    member = await create_test_user(client, "revoke_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Revoke Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create custom role
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Revokable Role",
            "description": "Will be revoked",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    custom_role_id = role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="Revoke_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="Revoke_Project")
    project = projects[0][0]
    project_id = project.id

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Grant access
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=custom_role_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Verify grant exists
    access_entries = resource_access_dao.get_resource_access("project", project_id)
    assert len(access_entries) == 1

    # Revoke access
    resource_access_dao.revoke_access(
        resource_type="project",
        resource_id=project_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Verify grant is removed
    access_entries_after = resource_access_dao.get_resource_access(
        "project",
        project_id,
    )
    assert len(access_entries_after) == 0


@pytest.mark.anyio
async def test_multiple_custom_roles_on_same_project(client: AsyncClient, dbsession):
    """Test granting multiple users different custom roles on same project."""
    owner = await create_test_user(client, "multi_role_owner@test.com")
    reader = await create_test_user(client, "multi_role_reader@test.com")
    writer = await create_test_user(client, "multi_role_writer@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Multi Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add members
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": reader["id"]},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": writer["id"]},
        headers=owner["headers"],
    )

    # Get permissions
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")
    project_write_id = next(
        p["id"] for p in permissions if p["name"] == "project:write"
    )

    # Create read-only role
    reader_role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Reader",
            "description": "Read only",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    reader_role_id = reader_role_response.json()["id"]

    # Create read-write role
    writer_role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Writer",
            "description": "Read and write",
            "permission_ids": [project_read_id, project_write_id],
        },
        headers=owner["headers"],
    )
    writer_role_id = writer_role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="MultiRole_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="MultiRole_Project")
    project = projects[0][0]
    project_id = project.id

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Grant different roles to different users
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=reader_role_id,
        grantee_type="user",
        grantee_id=reader["id"],
    )
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=writer_role_id,
        grantee_type="user",
        grantee_id=writer["id"],
    )
    dbsession.commit()

    # Clear cache
    ResourceAccessDAO.clear_permission_cache()

    # Verify reader has read but not write
    assert resource_access_dao.check_user_permission(
        reader["id"],
        "project",
        project_id,
        "project:read",
    )
    assert not resource_access_dao.check_user_permission(
        reader["id"],
        "project",
        project_id,
        "project:write",
    )

    # Verify writer has both read and write
    assert resource_access_dao.check_user_permission(
        writer["id"],
        "project",
        project_id,
        "project:read",
    )
    assert resource_access_dao.check_user_permission(
        writer["id"],
        "project",
        project_id,
        "project:write",
    )


@pytest.mark.anyio
async def test_owner_always_has_full_access_despite_explicit_grants(
    client: AsyncClient,
    dbsession,
):
    """Test that organization owner always has full access even when explicit grants exist."""
    owner = await create_test_user(client, "owner_full_owner@test.com")
    member = await create_test_user(client, "owner_full_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Owner Full Access Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create restrictive custom role (read only)
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Very Restricted",
            "description": "Minimal access",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    custom_role_id = role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="OwnerAccess_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="OwnerAccess_Project")
    project = projects[0][0]
    project_id = project.id

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Grant explicit access to member only (not owner)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=custom_role_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    ResourceAccessDAO.clear_permission_cache()

    # Owner should still have full access (all permissions)
    assert resource_access_dao.check_user_permission(
        owner["id"],
        "project",
        project_id,
        "project:read",
    )
    assert resource_access_dao.check_user_permission(
        owner["id"],
        "project",
        project_id,
        "project:write",
    )
    assert resource_access_dao.check_user_permission(
        owner["id"],
        "project",
        project_id,
        "project:delete",
    )


@pytest.mark.anyio
async def test_user_without_explicit_grant_has_no_access_when_grants_exist(
    client: AsyncClient,
    dbsession,
):
    """Test that user without explicit grant has no access when resource has grants."""
    owner = await create_test_user(client, "no_grant_owner@test.com")
    granted_user = await create_test_user(client, "no_grant_granted@test.com")
    not_granted_user = await create_test_user(client, "no_grant_not@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "No Grant Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add both users as members
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": granted_user["id"]},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": not_granted_user["id"]},
        headers=owner["headers"],
    )

    # Create custom role
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")

    role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Specific Access Role",
            "description": "For specific users only",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    custom_role_id = role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="Specific_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="Specific_Project")
    project = projects[0][0]
    project_id = project.id

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Grant access ONLY to granted_user
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=custom_role_id,
        grantee_type="user",
        grantee_id=granted_user["id"],
    )
    dbsession.commit()

    ResourceAccessDAO.clear_permission_cache()

    # granted_user should have access
    assert resource_access_dao.check_user_permission(
        granted_user["id"],
        "project",
        project_id,
        "project:read",
    )

    # not_granted_user should NOT have access (even though they're an org member)
    # Because explicit grants exist, implicit membership is not used
    assert not resource_access_dao.check_user_permission(
        not_granted_user["id"],
        "project",
        project_id,
        "project:read",
    )


@pytest.mark.anyio
async def test_update_resource_access_role(client: AsyncClient, dbsession):
    """Test updating the role of an existing ResourceAccess grant."""
    owner = await create_test_user(client, "update_access_owner@test.com")
    member = await create_test_user(client, "update_access_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Update Access Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Get permissions
    perms_response = await client.get("/v0/permissions", headers=owner["headers"])
    permissions = perms_response.json()
    project_read_id = next(p["id"] for p in permissions if p["name"] == "project:read")
    project_write_id = next(
        p["id"] for p in permissions if p["name"] == "project:write"
    )

    # Create read-only role
    reader_role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Initial Reader",
            "description": "Read only initially",
            "permission_ids": [project_read_id],
        },
        headers=owner["headers"],
    )
    reader_role_id = reader_role_response.json()["id"]

    # Create read-write role
    writer_role_response = await client.post(
        f"/v0/organizations/{org_id}/roles",
        json={
            "name": "Upgraded Writer",
            "description": "Read and write after upgrade",
            "permission_ids": [project_read_id, project_write_id],
        },
        headers=owner["headers"],
    )
    writer_role_id = writer_role_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=None,
        name="UpdateAccess_Project",
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="UpdateAccess_Project")
    project = projects[0][0]
    project_id = project.id

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Grant initial read-only access
    access = resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project_id,
        role_id=reader_role_id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    access_id = access.id
    dbsession.commit()

    ResourceAccessDAO.clear_permission_cache()

    # Verify initial permissions
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    )
    assert not resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    )

    # Update to writer role
    updated_access = resource_access_dao.update_role(access_id, writer_role_id)
    dbsession.commit()

    assert updated_access is not None
    assert updated_access.role_id == writer_role_id

    ResourceAccessDAO.clear_permission_cache()

    # Verify upgraded permissions
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    )
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    )
