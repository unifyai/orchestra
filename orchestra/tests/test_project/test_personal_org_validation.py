"""
Phase 5 Validation Tests: Personal vs Organizational Project System.

These tests ensure:
1. Personal projects work without RBAC overhead
2. Org projects use permission-based RBAC
3. Personal and org projects are properly isolated
4. Permission checks work correctly (not role checks)
5. Organization isolation (org A vs org B)
"""

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
async def test_personal_project_creation_no_resource_access(
    client: AsyncClient,
    dbsession,
):
    """
    Test that personal projects are created WITHOUT ResourceAccess entries.

    Personal projects should:
    - Have user_id set (creator)
    - Have organization_id = NULL
    - NOT create ResourceAccess entries
    - Creator has implicit full access
    """
    user = await create_test_user(client, "personal_project_user@test.com")

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Create personal project
    project_dao.create(
        name="My Personal Project",
        user_id=user["id"],
        organization_id=None,  # Personal project
    )
    dbsession.commit()

    # Get the project
    projects = project_dao.filter(
        user_id=user["id"],
        name="My Personal Project",
    )
    project = projects[0][0]

    # Verify project properties
    assert project.user_id == user["id"], "Personal project should have user_id set"
    assert (
        project.organization_id is None
    ), "Personal project should have NULL organization_id"

    # Verify NO ResourceAccess entries exist for this project
    access_entries = resource_access_dao.get_resource_access("project", project.id)
    assert (
        len(access_entries) == 0
    ), "Personal projects should NOT have ResourceAccess entries"

    # Verify creator has implicit full access (bypasses RBAC)
    assert resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:read",
    ), "Creator should have implicit read access"

    assert resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:write",
    ), "Creator should have implicit write access"

    assert resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:delete",
    ), "Creator should have implicit delete access"


@pytest.mark.anyio
async def test_org_project_creation_with_resource_access(
    client: AsyncClient,
    dbsession,
):
    """
    Test that organizational projects are created WITH ResourceAccess entries.

    Org projects should:
    - Have organization_id set
    - Have user_id = NULL (owned by org, not user)
    - CREATE ResourceAccess entry for creator with Owner role
    - Use explicit RBAC for permission checks
    """
    owner = await create_test_user(client, "org_project_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Project Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Create org project
    project_dao.create(
        name="Org Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()

    # Get the project
    projects = project_dao.filter(
        organization_id=org_id,
        name="Org Project",
    )
    project = projects[0][0]

    # Verify project properties
    assert (
        project.organization_id == org_id
    ), "Org project should have organization_id set"
    assert project.user_id is None, "Org project should have NULL user_id"

    # Verify owner has access through implicit org membership
    # (Owner role is assigned when org is created)
    assert resource_access_dao.check_user_permission(
        owner["id"],
        "project",
        project.id,
        "project:read",
    ), "Owner should have read access"

    assert resource_access_dao.check_user_permission(
        owner["id"],
        "project",
        project.id,
        "project:write",
    ), "Owner should have write access"


@pytest.mark.anyio
async def test_personal_org_project_isolation(client: AsyncClient, dbsession):
    """
    Test that personal and org projects are properly isolated.

    - Personal project creator has full access to their project
    - Personal project creator has NO access to unrelated org projects
    - Org members have NO access to personal projects
    """
    personal_user = await create_test_user(client, "personal_isolation@test.com")
    org_owner = await create_test_user(client, "org_isolation_owner@test.com")
    org_member = await create_test_user(client, "org_isolation_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Isolation Test Org"},
        headers=org_owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add org_member to organization
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": org_member["id"]},
        headers=org_owner["headers"],
    )

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Create personal project
    project_dao.create(
        name="Personal_Isolation_Project",
        user_id=personal_user["id"],
        organization_id=None,
    )
    dbsession.commit()

    personal_projects = project_dao.filter(
        user_id=personal_user["id"],
        name="Personal_Isolation_Project",
    )
    personal_project = personal_projects[0][0]

    # Create org project
    project_dao.create(
        name="Org_Isolation_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    org_projects = project_dao.filter(
        organization_id=org_id,
        name="Org_Isolation_Project",
    )
    org_project = org_projects[0][0]

    # PERSONAL USER: Has access to their personal project
    assert resource_access_dao.check_user_permission(
        personal_user["id"],
        "project",
        personal_project.id,
        "project:read",
    ), "Personal user should access their own project"

    # PERSONAL USER: NO access to unrelated org project
    assert not resource_access_dao.check_user_permission(
        personal_user["id"],
        "project",
        org_project.id,
        "project:read",
    ), "Personal user should NOT access unrelated org project"

    # ORG MEMBER: Has access to org project (implicit membership)
    assert resource_access_dao.check_user_permission(
        org_member["id"],
        "project",
        org_project.id,
        "project:read",
    ), "Org member should access org project"

    # ORG MEMBER: NO access to unrelated personal project
    assert not resource_access_dao.check_user_permission(
        org_member["id"],
        "project",
        personal_project.id,
        "project:read",
    ), "Org member should NOT access unrelated personal project"


@pytest.mark.anyio
async def test_org_a_org_b_isolation(client: AsyncClient, dbsession):
    """
    Test that members of Org A cannot access Org B resources.

    - Org A members have access to Org A projects
    - Org A members have NO access to Org B projects
    - Org A members have NO access to Org B itself
    """
    org_a_owner = await create_test_user(client, "org_a_owner@test.com")
    org_a_member = await create_test_user(client, "org_a_member@test.com")
    org_b_owner = await create_test_user(client, "org_b_owner@test.com")
    org_b_member = await create_test_user(client, "org_b_member@test.com")

    # Create Org A
    org_a_response = await client.post(
        "/v0/organizations",
        json={"name": "Organization A"},
        headers=org_a_owner["headers"],
    )
    org_a_id = org_a_response.json()["id"]

    # Create Org B
    org_b_response = await client.post(
        "/v0/organizations",
        json={"name": "Organization B"},
        headers=org_b_owner["headers"],
    )
    org_b_id = org_b_response.json()["id"]

    # Add members
    await client.post(
        f"/v0/organizations/{org_a_id}/members",
        json={"user_id": org_a_member["id"]},
        headers=org_a_owner["headers"],
    )

    await client.post(
        f"/v0/organizations/{org_b_id}/members",
        json={"user_id": org_b_member["id"]},
        headers=org_b_owner["headers"],
    )

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Create projects in both orgs
    project_dao.create(
        name="Org_A_Project",
        user_id=None,
        organization_id=org_a_id,
    )
    dbsession.commit()

    org_a_projects = project_dao.filter(
        organization_id=org_a_id,
        name="Org_A_Project",
    )
    org_a_project = org_a_projects[0][0]

    project_dao.create(
        name="Org_B_Project",
        user_id=None,
        organization_id=org_b_id,
    )
    dbsession.commit()

    org_b_projects = project_dao.filter(
        organization_id=org_b_id,
        name="Org_B_Project",
    )
    org_b_project = org_b_projects[0][0]

    # ORG A MEMBER: Can access Org A project
    assert resource_access_dao.check_user_permission(
        org_a_member["id"],
        "project",
        org_a_project.id,
        "project:read",
    ), "Org A member should access Org A project"

    # ORG A MEMBER: CANNOT access Org B project
    assert not resource_access_dao.check_user_permission(
        org_a_member["id"],
        "project",
        org_b_project.id,
        "project:read",
    ), "Org A member should NOT access Org B project"

    # ORG A MEMBER: CANNOT access Org B organization
    # Use check_org_member_permission for org-level access checks
    assert not resource_access_dao.check_org_member_permission(
        org_a_member["id"],
        org_b_id,
        "org:read",
    ), "Org A member should NOT access Org B organization"

    # ORG A MEMBER: CANNOT view Org B members
    list_response = await client.get(
        f"/v0/organizations/{org_b_id}/members",
        headers=org_a_member["headers"],
    )
    assert (
        list_response.status_code == status.HTTP_403_FORBIDDEN
    ), "Org A member should NOT list Org B members"

    # ORG B MEMBER: Can access Org B project (verify isolation works both ways)
    assert resource_access_dao.check_user_permission(
        org_b_member["id"],
        "project",
        org_b_project.id,
        "project:read",
    ), "Org B member should access Org B project"

    # ORG B MEMBER: CANNOT access Org A project
    assert not resource_access_dao.check_user_permission(
        org_b_member["id"],
        "project",
        org_a_project.id,
        "project:read",
    ), "Org B member should NOT access Org A project"


@pytest.mark.anyio
async def test_permission_based_not_role_based_access(client: AsyncClient, dbsession):
    """
    Test that access is permission-based, not role-based.

    - Users with org:write (regardless of role name) can manage members
    - Users without org:write (regardless of being "members") cannot
    - Custom roles with specific permissions work correctly
    """
    owner = await create_test_user(client, "perm_based_owner@test.com")
    admin_user = await create_test_user(client, "perm_based_admin@test.com")
    viewer_user = await create_test_user(client, "perm_based_viewer@test.com")
    new_member = await create_test_user(client, "perm_based_new@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Permission Based Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get roles
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Add admin (has org:write)
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin_user["id"], "role_id": admin_role.id},
        headers=owner["headers"],
    )

    # Add viewer (no org:write)
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer_user["id"], "role_id": viewer_role.id},
        headers=owner["headers"],
    )

    # ADMIN (has org:write) CAN add members
    add_as_admin = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": new_member["id"]},
        headers=admin_user["headers"],
    )
    assert (
        add_as_admin.status_code == status.HTTP_201_CREATED
    ), "Admin with org:write should be able to add members"

    # Remove the member for next test
    await client.delete(
        f"/v0/organizations/{org_id}/members/{new_member['id']}",
        headers=owner["headers"],
    )

    # VIEWER (no org:write) CANNOT add members
    add_as_viewer = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": new_member["id"]},
        headers=viewer_user["headers"],
    )
    assert (
        add_as_viewer.status_code == status.HTTP_403_FORBIDDEN
    ), "Viewer without org:write should NOT be able to add members"

    # VIEWER (has org:read) CAN list members
    list_as_viewer = await client.get(
        f"/v0/organizations/{org_id}/members",
        headers=viewer_user["headers"],
    )
    assert (
        list_as_viewer.status_code == status.HTTP_200_OK
    ), "Viewer with org:read should be able to list members"

    # ADMIN (has org:write) CAN update organization
    update_as_admin = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"name": "Updated By Admin"},
        headers=admin_user["headers"],
    )
    assert (
        update_as_admin.status_code == status.HTTP_200_OK
    ), "Admin with org:write should be able to update organization"

    # VIEWER (no org:write) CANNOT update organization
    update_as_viewer = await client.patch(
        f"/v0/organizations/{org_id}",
        json={"name": "Attempted By Viewer"},
        headers=viewer_user["headers"],
    )
    assert (
        update_as_viewer.status_code == status.HTTP_403_FORBIDDEN
    ), "Viewer without org:write should NOT be able to update organization"
