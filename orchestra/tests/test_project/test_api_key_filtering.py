"""
Tests for personal vs organization API key project filtering.

These tests verify that:
1. Personal API key only shows/accesses personal projects
2. Organization API key only shows/accesses that organization's projects
3. Projects with explicit ResourceAccess grants are correctly included
4. Logging respects API key context
"""

import pytest
from httpx import AsyncClient
from starlette import status

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.tests.utils import create_test_user

# ==================== Project Listing Tests ====================


@pytest.mark.anyio
async def test_list_projects_personal_api_key_shows_only_personal(
    client: AsyncClient,
    dbsession,
):
    """Personal API key should only show personal projects."""
    user = await create_test_user(client, "personal_list@test.com")

    # Create a personal project
    response = await client.post(
        "/v0/project",
        json={"name": "PersonalProject"},
        headers=user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # List projects with personal API key
    response = await client.get("/v0/projects", headers=user["headers"])
    assert response.status_code == status.HTTP_200_OK
    projects = response.json()

    # Should contain the personal project
    assert "PersonalProject" in projects


@pytest.mark.anyio
async def test_list_projects_org_api_key_shows_only_org_projects(
    client: AsyncClient,
    dbsession,
):
    """Organization API key should only show that organization's projects."""
    owner = await create_test_user(client, "org_list_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "ListTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create a personal project with personal API key
    response = await client.post(
        "/v0/project",
        json={"name": "PersonalProjectForOrgTest"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    # Create an org project with org API key
    response = await client.post(
        "/v0/project",
        json={"name": "OrgProject"},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK

    # List projects with org API key
    response = await client.get("/v0/projects", headers=org_headers)
    assert response.status_code == status.HTTP_200_OK
    org_projects = response.json()

    # Should contain org project, but NOT personal project
    assert "OrgProject" in org_projects
    assert "PersonalProjectForOrgTest" not in org_projects

    # List projects with personal API key
    response = await client.get("/v0/projects", headers=owner["headers"])
    assert response.status_code == status.HTTP_200_OK
    personal_projects = response.json()

    # Should contain personal project, but NOT org project
    assert "PersonalProjectForOrgTest" in personal_projects
    assert "OrgProject" not in personal_projects


@pytest.mark.anyio
async def test_list_projects_tree_respects_api_key_context(
    client: AsyncClient,
    dbsession,
):
    """Projects tree endpoint should also respect API key context."""
    owner = await create_test_user(client, "tree_test_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "TreeTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create personal and org projects
    await client.post(
        "/v0/project",
        json={"name": "TreePersonal"},
        headers=owner["headers"],
    )
    await client.post(
        "/v0/project",
        json={"name": "TreeOrg"},
        headers=org_headers,
    )

    # Check tree with personal API key
    response = await client.get("/v0/projects/tree", headers=owner["headers"])
    assert response.status_code == status.HTTP_200_OK
    personal_tree = response.json()
    personal_names = [p["project"] for p in personal_tree]  # tree uses "project" key
    assert "TreePersonal" in personal_names
    assert "TreeOrg" not in personal_names

    # Check tree with org API key
    response = await client.get("/v0/projects/tree", headers=org_headers)
    assert response.status_code == status.HTTP_200_OK
    org_tree = response.json()
    org_names = [p["project"] for p in org_tree]  # tree uses "project" key
    assert "TreeOrg" in org_names
    assert "TreePersonal" not in org_names


# ==================== Project Access Tests ====================


@pytest.mark.anyio
async def test_access_personal_project_with_org_api_key_fails(
    client: AsyncClient,
    dbsession,
):
    """Using org API key to access personal project should fail."""
    owner = await create_test_user(client, "access_test_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "AccessTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create personal project
    await client.post(
        "/v0/project",
        json={"name": "PersonalAccessTest"},
        headers=owner["headers"],
    )

    # Try to access personal project with org API key
    response = await client.get(
        "/v0/project/PersonalAccessTest",
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_access_org_project_with_personal_api_key_fails(
    client: AsyncClient,
    dbsession,
):
    """Using personal API key to access org project should fail."""
    owner = await create_test_user(client, "org_access_test@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "OrgAccessTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create org project
    await client.post(
        "/v0/project",
        json={"name": "OrgAccessTest"},
        headers=org_headers,
    )

    # Try to access org project with personal API key
    response = await client.get(
        "/v0/project/OrgAccessTest",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ==================== Logging Tests ====================


@pytest.mark.anyio
async def test_log_to_personal_project_with_org_api_key_fails(
    client: AsyncClient,
    dbsession,
):
    """Using org API key to log to personal project should fail."""
    owner = await create_test_user(client, "log_access_test@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "LogAccessTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create personal project
    await client.post(
        "/v0/project",
        json={"name": "PersonalLogTest"},
        headers=owner["headers"],
    )

    # Try to log to personal project with org API key
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": "PersonalLogTest",
            "entries": [{"data": {"test": "value"}}],
        },
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_log_to_org_project_with_personal_api_key_fails(
    client: AsyncClient,
    dbsession,
):
    """Using personal API key to log to org project should fail."""
    owner = await create_test_user(client, "org_log_test@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "OrgLogTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create org project
    await client.post(
        "/v0/project",
        json={"name": "OrgLogTest"},
        headers=org_headers,
    )

    # Try to log to org project with personal API key
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": "OrgLogTest",
            "entries": [{"data": {"test": "value"}}],
        },
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_log_to_org_project_with_org_api_key_succeeds(
    client: AsyncClient,
    dbsession,
):
    """Using org API key to log to org project should succeed."""
    owner = await create_test_user(client, "org_log_success@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "OrgLogSuccessOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create org project
    await client.post(
        "/v0/project",
        json={"name": "OrgLogSuccessTest"},
        headers=org_headers,
    )

    # Log to org project with org API key
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": "OrgLogSuccessTest",
            "entries": [{"data": {"test": "value"}}],
        },
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK


# ==================== ResourceAccess Grant Tests ====================


@pytest.mark.anyio
async def test_project_with_explicit_grant_appears_in_listing(
    client: AsyncClient,
    dbsession,
):
    """Projects with explicit ResourceAccess grants should appear in listings."""
    owner = await create_test_user(client, "grant_list_owner@test.com")
    member = await create_test_user(client, "grant_list_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "GrantListOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    owner_org_api_key = org_data["api_key"]

    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_api_key}",
    }

    # Add member to org
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED
    member_org_api_key = add_member_response.json()["api_key"]

    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {member_org_api_key}",
    }

    # Create org project
    await client.post(
        "/v0/project",
        json={"name": "GrantedProject"},
        headers=owner_org_headers,
    )

    # Grant explicit access to member
    # Get project ID
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    projects = project_dao.filter(organization_id=org_id, name="GrantedProject")
    assert len(projects) > 0
    project = projects[0][0]

    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Member should see the project in listing
    response = await client.get("/v0/projects", headers=member_org_headers)
    assert response.status_code == status.HTTP_200_OK
    projects = response.json()
    assert "GrantedProject" in projects


# ==================== Context Tests ====================


@pytest.mark.anyio
async def test_create_context_with_wrong_api_key_fails(
    client: AsyncClient,
    dbsession,
):
    """Creating context in project with wrong API key type should fail."""
    owner = await create_test_user(client, "context_test@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "ContextTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create personal project
    await client.post(
        "/v0/project",
        json={"name": "PersonalContextTest"},
        headers=owner["headers"],
    )

    # Try to create context in personal project with org API key
    response = await client.post(
        "/v0/project/PersonalContextTest/contexts",
        json="TestContext",
        headers=org_headers,
    )
    # Should fail because project not accessible with org API key
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ==================== Explicit Grant Required Tests (Option B) ====================


@pytest.mark.anyio
async def test_org_member_without_grant_cannot_see_project(
    client: AsyncClient,
    dbsession,
):
    """Org member without explicit ResourceAccess grant cannot see org projects."""
    owner = await create_test_user(client, "no_grant_owner@test.com")
    member = await create_test_user(client, "no_grant_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "NoGrantOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    owner_org_api_key = org_data["api_key"]

    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_api_key}",
    }

    # Add member to org (but no project grant will be given)
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED
    member_org_api_key = add_member_response.json()["api_key"]

    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {member_org_api_key}",
    }

    # Owner creates org project (owner gets Owner grant automatically)
    await client.post(
        "/v0/project",
        json={"name": "OwnerOnlyProject"},
        headers=owner_org_headers,
    )

    # Member lists projects - should NOT see the project (no grant)
    response = await client.get("/v0/projects", headers=member_org_headers)
    assert response.status_code == status.HTTP_200_OK
    projects = response.json()
    assert "OwnerOnlyProject" not in projects

    # Owner can see the project
    response = await client.get("/v0/projects", headers=owner_org_headers)
    assert response.status_code == status.HTTP_200_OK
    projects = response.json()
    assert "OwnerOnlyProject" in projects


@pytest.mark.anyio
async def test_org_member_without_grant_cannot_log_to_project(
    client: AsyncClient,
    dbsession,
):
    """Org member without explicit grant cannot log to org project."""
    owner = await create_test_user(client, "log_no_access_owner@test.com")
    member = await create_test_user(client, "log_no_access_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "LogNoAccessOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    owner_org_api_key = org_data["api_key"]

    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_api_key}",
    }

    # Add member to org (no project grant)
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED
    member_org_api_key = add_member_response.json()["api_key"]

    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {member_org_api_key}",
    }

    # Owner creates project
    await client.post(
        "/v0/project",
        json={"name": "OwnerOnlyLogProject"},
        headers=owner_org_headers,
    )

    # Member tries to log - should fail with 404 (project not found)
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": "OwnerOnlyLogProject",
            "entries": [{"data": {"test": "value"}}],
        },
        headers=member_org_headers,
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_org_member_with_grant_can_log_to_project(
    client: AsyncClient,
    dbsession,
):
    """Org member with explicit grant can log to org project."""
    owner = await create_test_user(client, "log_with_access_owner@test.com")
    member = await create_test_user(client, "log_with_access_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "LogWithAccessOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    owner_org_api_key = org_data["api_key"]

    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_api_key}",
    }

    # Add member to org
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED
    member_org_api_key = add_member_response.json()["api_key"]

    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {member_org_api_key}",
    }

    # Owner creates project
    await client.post(
        "/v0/project",
        json={"name": "SharedLogProject"},
        headers=owner_org_headers,
    )

    # Grant Member access to the project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    projects = project_dao.filter(organization_id=org_id, name="SharedLogProject")
    project = projects[0][0]

    member_role = role_dao.get_by_name("Member", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=member_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Member can now log to the project
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": "SharedLogProject",
            "entries": [{"data": {"test": "value"}}],
        },
        headers=member_org_headers,
    )
    assert response.status_code == status.HTTP_200_OK


@pytest.mark.anyio
async def test_viewer_can_read_but_not_write_logs(
    client: AsyncClient,
    dbsession,
):
    """Viewer role can read logs but cannot write to project."""
    owner = await create_test_user(client, "viewer_test_owner@test.com")
    viewer = await create_test_user(client, "viewer_test_viewer@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "ViewerTestOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    owner_org_api_key = org_data["api_key"]

    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_api_key}",
    }

    # Add viewer to org
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED
    viewer_org_api_key = add_member_response.json()["api_key"]

    viewer_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {viewer_org_api_key}",
    }

    # Owner creates project and adds some logs
    await client.post(
        "/v0/project",
        json={"name": "ViewerTestProject"},
        headers=owner_org_headers,
    )
    await client.post(
        "/v0/logs",
        json={
            "project_name": "ViewerTestProject",
            "entries": [{"data": {"owner_log": "value"}}],
        },
        headers=owner_org_headers,
    )

    # Grant Viewer access to the project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    projects = project_dao.filter(organization_id=org_id, name="ViewerTestProject")
    project = projects[0][0]

    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=viewer["id"],
    )
    dbsession.commit()

    # Viewer can see the project
    response = await client.get("/v0/projects", headers=viewer_org_headers)
    assert response.status_code == status.HTTP_200_OK
    projects = response.json()
    assert "ViewerTestProject" in projects

    # Viewer can query logs
    response = await client.post(
        "/v0/logs/query",
        json={"project_name": "ViewerTestProject"},
        headers=viewer_org_headers,
    )
    assert response.status_code == status.HTTP_200_OK

    # Viewer cannot write logs (should get 403 - permission denied)
    response = await client.post(
        "/v0/logs",
        json={
            "project_name": "ViewerTestProject",
            "entries": [{"data": {"viewer_log": "value"}}],
        },
        headers=viewer_org_headers,
    )
    # Viewers don't have project:write permission
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "permission" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_get_user_resource_access_endpoint(
    client: AsyncClient,
    dbsession,
):
    """Test the new endpoint for getting a user's access on a project."""
    owner = await create_test_user(client, "user_access_owner@test.com")
    member = await create_test_user(client, "user_access_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "UserAccessOrg"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    owner_org_api_key = org_data["api_key"]

    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_api_key}",
    }

    # Add member to org
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED

    # Owner creates project
    await client.post(
        "/v0/project",
        json={"name": "UserAccessProject"},
        headers=owner_org_headers,
    )

    # Get project ID
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    projects = project_dao.filter(organization_id=org_id, name="UserAccessProject")
    project = projects[0][0]

    # Grant Member access to member
    member_role = role_dao.get_by_name("Member", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=member_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Owner checks their own access
    response = await client.get(
        f"/v0/resources/project/{project.id}/access/user/{owner['id']}",
        headers=owner_org_headers,
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["user_id"] == owner["id"]
    assert data["effective_role"] == "Owner"
    assert len(data["access_entries"]) == 1
    assert data["access_entries"][0]["role_name"] == "Owner"
    # Verify permissions are included
    assert "permissions" in data["access_entries"][0]
    assert isinstance(data["access_entries"][0]["permissions"], list)

    # Owner checks member's access
    response = await client.get(
        f"/v0/resources/project/{project.id}/access/user/{member['id']}",
        headers=owner_org_headers,
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["user_id"] == member["id"]
    assert data["effective_role"] == "Member"
    assert len(data["access_entries"]) == 1
    assert data["access_entries"][0]["role_name"] == "Member"
    # Verify permissions are included for member
    assert "permissions" in data["access_entries"][0]
    assert isinstance(data["access_entries"][0]["permissions"], list)
