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
            "project": "PersonalLogTest",
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
            "project": "OrgLogTest",
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
            "project": "OrgLogSuccessTest",
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
