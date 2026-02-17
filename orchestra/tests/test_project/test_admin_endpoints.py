"""
Tests for admin project endpoints:
1. admin_list_org_projects - List all org projects (bypass RBAC)
2. admin_delete_project - Delete any project by ID
3. admin_grant_resource_access - Grant access to any resource
"""

import pytest
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_admin_list_org_projects(client: AsyncClient, dbsession):
    """
    Test that admin can list ALL projects in an org, bypassing RBAC.

    This should:
    - Return all org projects regardless of ResourceAccess grants
    - Include orphaned projects with no grants
    """
    owner = await create_test_user(client, "admin_list_org_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Admin List Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    # Create org project WITH ResourceAccess (normal flow)
    project_dao.create(
        name="Visible_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Create org project WITHOUT ResourceAccess (orphaned)
    project_dao.create(
        name="Orphaned_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Admin endpoint should see BOTH projects (bypasses RBAC)
    admin_response = await client.get(
        f"/v0/admin/projects/org/{org_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin_response.status_code == 200
    projects = admin_response.json()

    project_names = [p["name"] for p in projects]
    assert "Visible_Project" in project_names, "Should see visible project"
    assert (
        "Orphaned_Project" in project_names
    ), "Should see orphaned project (bypasses RBAC)"


@pytest.mark.anyio
async def test_admin_list_org_projects_empty(client: AsyncClient, dbsession):
    """Test admin list org projects returns empty list for org with no projects."""
    owner = await create_test_user(client, "admin_list_empty_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Empty Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Admin endpoint should return empty list
    admin_response = await client.get(
        f"/v0/admin/projects/org/{org_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin_response.status_code == 200
    assert admin_response.json() == []


@pytest.mark.anyio
async def test_admin_delete_project(client: AsyncClient, dbsession):
    """
    Test that admin can delete any project by ID.

    This should:
    - Delete the project regardless of ownership
    - Remove any ResourceAccess grants
    - Return project info in response
    """
    owner = await create_test_user(client, "admin_delete_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Admin Delete Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    # Create org project
    project_dao.create(
        name="Project_To_Delete",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Get project ID
    projects = project_dao.filter(organization_id=org_id, name="Project_To_Delete")
    project_id = projects[0][0].id

    # Admin delete the project
    delete_response = await client.delete(
        f"/v0/admin/project/{project_id}",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 200
    data = delete_response.json()
    assert "deleted successfully" in data["info"]
    assert data["organization_id"] == org_id

    # Verify project is gone
    remaining = project_dao.filter(organization_id=org_id, name="Project_To_Delete")
    assert len(remaining) == 0, "Project should be deleted"


@pytest.mark.anyio
async def test_admin_delete_project_not_found(client: AsyncClient):
    """Test admin delete returns 404 for non-existent project."""
    delete_response = await client.delete(
        "/v0/admin/project/999999",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 404
    assert "not found" in delete_response.json()["detail"]


@pytest.mark.anyio
async def test_admin_delete_orphaned_project(client: AsyncClient, dbsession):
    """
    Test that admin can delete an orphaned project (no ResourceAccess).

    This is the key use case - cleaning up projects that have no grants.
    """
    owner = await create_test_user(client, "admin_delete_orphan_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Admin Delete Orphan Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    # Create orphaned project (no ResourceAccess grants)
    project_dao.create(
        name="Orphaned_Project_To_Delete",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Get project ID
    projects = project_dao.filter(
        organization_id=org_id,
        name="Orphaned_Project_To_Delete",
    )
    project_id = projects[0][0].id

    # Admin can delete even without any grants existing
    delete_response = await client.delete(
        f"/v0/admin/project/{project_id}",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 200

    # Verify project is gone
    remaining = project_dao.filter(
        organization_id=org_id,
        name="Orphaned_Project_To_Delete",
    )
    assert len(remaining) == 0


@pytest.mark.anyio
async def test_admin_grant_resource_access(client: AsyncClient, dbsession):
    """
    Test that admin can grant access to any resource.

    This should:
    - Work without permission checks
    - Create ResourceAccess entry
    - Allow user to then access the resource
    """
    owner = await create_test_user(client, "admin_grant_owner@test.com")
    viewer = await create_test_user(client, "admin_grant_viewer@test.com")

    # Create organization and add viewer as member
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Admin Grant Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add viewer to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"email": "admin_grant_viewer@test.com"},
        headers=owner["headers"],
    )

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    role_dao = RoleDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Create orphaned org project (no grants)
    project_dao.create(
        name="Orphaned_Grant_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Get project ID
    projects = project_dao.filter(
        organization_id=org_id,
        name="Orphaned_Grant_Project",
    )
    project_id = projects[0][0].id

    # Verify viewer has no access initially
    assert not resource_access_dao.check_user_permission(
        viewer["id"],
        "project",
        project_id,
        "project:read",
    ), "Viewer should not have access initially"

    # Get Viewer role ID
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    assert viewer_role is not None, "Viewer role should exist"

    # Admin grants access
    grant_response = await client.post(
        "/v0/admin/resources/access",
        json={
            "resource_type": "project",
            "resource_id": project_id,
            "role_id": viewer_role.id,
            "grantee_type": "user",
            "grantee_id": viewer["id"],
        },
        headers=ADMIN_HEADERS,
    )
    assert grant_response.status_code == 201
    data = grant_response.json()
    assert data["info"] == "Access granted successfully"
    assert data["resource_type"] == "project"
    assert data["resource_id"] == project_id
    assert data["grantee_id"] == viewer["id"]

    # Verify viewer now has access
    dbsession.expire_all()  # Refresh session to see changes
    assert resource_access_dao.check_user_permission(
        viewer["id"],
        "project",
        project_id,
        "project:read",
    ), "Viewer should have access after admin grant"


@pytest.mark.anyio
async def test_admin_grant_resource_access_invalid_role(client: AsyncClient, dbsession):
    """Test admin grant returns 404 for non-existent role."""
    owner = await create_test_user(client, "admin_grant_invalid_role@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Admin Grant Invalid Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    # Create project
    project_dao.create(
        name="Grant_Invalid_Role_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(
        organization_id=org_id,
        name="Grant_Invalid_Role_Project",
    )
    project_id = projects[0][0].id

    # Try to grant with invalid role ID
    grant_response = await client.post(
        "/v0/admin/resources/access",
        json={
            "resource_type": "project",
            "resource_id": project_id,
            "role_id": 99999,  # Non-existent role
            "grantee_type": "user",
            "grantee_id": owner["id"],
        },
        headers=ADMIN_HEADERS,
    )
    assert grant_response.status_code == 404
    assert "Role with id 99999 not found" in grant_response.json()["detail"]


@pytest.mark.anyio
async def test_admin_endpoints_fix_orphaned_project_workflow(
    client: AsyncClient,
    dbsession,
):
    """
    Integration test: Full workflow for fixing an orphaned project.

    1. Create an org project (simulating orphaned state)
    2. Admin lists org projects - finds the orphaned one
    3. Admin grants access to a user
    4. User can now access the project
    """
    owner = await create_test_user(client, "orphan_workflow_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Orphan Workflow Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    role_dao = RoleDAO(dbsession)

    # Step 1: Create orphaned project (no ResourceAccess)
    project_dao.create(
        name="Assistants",  # The problematic project name
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Step 2: Admin finds the orphaned project
    list_response = await client.get(
        f"/v0/admin/projects/org/{org_id}",
        headers=ADMIN_HEADERS,
    )
    assert list_response.status_code == 200
    projects = list_response.json()
    orphaned = next((p for p in projects if p["name"] == "Assistants"), None)
    assert orphaned is not None, "Admin should find orphaned Assistants project"
    project_id = orphaned["id"]

    # Step 3: Admin grants Owner access to the org owner
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    grant_response = await client.post(
        "/v0/admin/resources/access",
        json={
            "resource_type": "project",
            "resource_id": project_id,
            "role_id": owner_role.id,
            "grantee_type": "user",
            "grantee_id": owner["id"],
        },
        headers=ADMIN_HEADERS,
    )
    assert grant_response.status_code == 201

    # Step 4: Verify user can now access via regular API
    # Get org API key
    user_info_resp = await client.get(
        f"/v0/admin/user/by-email?email=orphan_workflow_owner@test.com",
        headers=ADMIN_HEADERS,
    )
    user_info = user_info_resp.json()
    org_api_key = None
    for org in user_info.get("organizations", []):
        if org.get("id") == org_id:
            org_api_key = org.get("api_key")
            break

    assert org_api_key is not None, "Should have org API key"

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # User can now list and see the project
    projects_response = await client.get("/v0/projects", headers=org_headers)
    assert projects_response.status_code == 200
    user_projects = projects_response.json()
    assert "Assistants" in user_projects, "User should now see Assistants project"
