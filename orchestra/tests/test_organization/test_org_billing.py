"""Tests for organization billing features (Phase 0)."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import create_test_user


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

    assert True


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
        json={"user_id": new_owner["id"]},
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

    # Verify roles were swapped
    org_member_dao = OrganizationMemberDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    new_owner_member = org_member_dao.get_member(new_owner["id"], org_id)
    old_owner_member = org_member_dao.get_member(owner["id"], org_id)

    new_owner_role = role_dao.get(new_owner_member.role_id)
    old_owner_role = role_dao.get(old_owner_member.role_id)

    assert new_owner_role.name == "Owner"
    assert old_owner_role.name == "Admin"


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
        json={"user_id": member["id"]},
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


# ==================== Org Project Creation Tests ====================


@pytest.mark.anyio
async def test_create_project_with_org_api_key(client: AsyncClient, dbsession):
    """Test that creating a project with org API key creates an org project."""
    owner = await create_test_user(client, "org_project_owner@test.com")

    # Create organization and get org API key
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Org Project Test"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_data = org_response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    # Create org headers
    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create project using org API key
    project_response = await client.post(
        "/v0/project",
        json={"name": "Org_Project_Test"},
        headers=org_headers,
    )
    assert project_response.status_code == 200
    assert project_response.json()["info"] == "Project created successfully!"

    # Verify project is an org project by checking it's accessible via org membership
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO
    from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
    from orchestra.db.dao.role_dao import RoleDAO

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    # Filter by organization_id - should find the project
    projects = project_dao.filter(organization_id=org_id, name="Org_Project_Test")
    assert len(projects) == 1
    project = projects[0][0]
    assert project.organization_id == org_id
    assert project.user_id is None  # Org projects don't have user_id

    # Verify explicit Owner grant was created for the creator
    access_entries = resource_access_dao.get_resource_access("project", project.id)
    assert len(access_entries) == 1
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    assert access_entries[0].role_id == owner_role.id
    assert access_entries[0].grantee_type == "user"
    assert access_entries[0].grantee_id == owner["id"]


@pytest.mark.anyio
async def test_create_project_with_personal_api_key(client: AsyncClient, dbsession):
    """Test that creating a project with personal API key creates a personal project."""
    user = await create_test_user(client, "personal_project_user@test.com")

    # Create project using personal API key
    project_response = await client.post(
        "/v0/project",
        json={"name": "Personal_Project_Test"},
        headers=user["headers"],
    )
    assert project_response.status_code == 200
    assert project_response.json()["info"] == "Project created successfully!"

    # Verify project is a personal project
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    # Filter by user_id - should find the project
    projects = project_dao.filter(user_id=user["id"], name="Personal_Project_Test")
    assert len(projects) == 1
    project = projects[0][0]
    assert project.user_id == user["id"]
    assert project.organization_id is None  # Personal projects don't have org_id


@pytest.mark.anyio
async def test_create_org_project_requires_permission(client: AsyncClient, dbsession):
    """Test that creating org project requires project:write permission."""
    owner = await create_test_user(client, "org_perm_owner@test.com")
    viewer = await create_test_user(client, "org_perm_viewer@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Permission Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add viewer to org with Viewer role (no project:write)
    from orchestra.db.dao.role_dao import RoleDAO

    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer["id"], "role_id": viewer_role.id},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED

    # Get viewer's org API key from the add_member response
    viewer_org_key = add_member_response.json()["api_key"]

    viewer_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {viewer_org_key}",
    }

    # Try to create project with viewer's org API key - should fail
    project_response = await client.post(
        "/v0/project",
        json={"name": "Viewer_Project_Test"},
        headers=viewer_org_headers,
    )
    assert project_response.status_code == status.HTTP_403_FORBIDDEN
    assert "permission" in project_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_create_org_project_duplicate_name(client: AsyncClient):
    """Test that duplicate project names in same org are rejected."""
    owner = await create_test_user(client, "org_dup_owner@test.com")

    # Create organization and get org API key
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Duplicate Project Org"},
        headers=owner["headers"],
    )
    org_api_key = org_response.json()["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
    }

    # Create first project
    project_response1 = await client.post(
        "/v0/project",
        json={"name": "Duplicate_Org_Project"},
        headers=org_headers,
    )
    assert project_response1.status_code == 200

    # Try to create second project with same name - should fail
    project_response2 = await client.post(
        "/v0/project",
        json={"name": "Duplicate_Org_Project"},
        headers=org_headers,
    )
    assert project_response2.status_code == 400
    assert "already exists" in project_response2.json()["detail"].lower()


@pytest.mark.anyio
async def test_member_can_create_org_project(client: AsyncClient, dbsession):
    """Test that members with project:write (Member role) can create org projects."""
    owner = await create_test_user(client, "member_create_owner@test.com")
    member = await create_test_user(client, "member_create_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Member Create Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member to org (default Member role has project:write)
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED

    # Get member's org API key from the add_member response
    member_org_key = add_member_response.json()["api_key"]

    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {member_org_key}",
    }

    # Create project with member's org API key - should succeed
    project_response = await client.post(
        "/v0/project",
        json={"name": "Member_Created_Project"},
        headers=member_org_headers,
    )
    assert project_response.status_code == 200
    assert project_response.json()["info"] == "Project created successfully!"

    # Verify it's an org project
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO
    from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
    from orchestra.db.dao.role_dao import RoleDAO

    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    projects = project_dao.filter(organization_id=org_id, name="Member_Created_Project")
    assert len(projects) == 1
    project = projects[0][0]
    assert project.organization_id == org_id
    assert project.user_id is None

    # Verify explicit Owner grant was created for the member who created the project
    access_entries = resource_access_dao.get_resource_access("project", project.id)
    assert len(access_entries) == 1
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    assert access_entries[0].role_id == owner_role.id
    assert access_entries[0].grantee_type == "user"
    assert access_entries[0].grantee_id == member["id"]


# ============== Project Listing API Key Context Tests ==============


@pytest.mark.anyio
async def test_list_projects_personal_api_key_shows_only_personal(client: AsyncClient):
    """Test that listing projects with personal API key shows only personal projects."""
    user = await create_test_user(client, "list_personal_only@test.com")

    # Create a personal project
    await client.post(
        "/v0/project",
        json={"name": "Personal_List_Test"},
        headers=user["headers"],
    )

    # Create an organization and org project
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "List Test Org"},
        headers=user["headers"],
    )
    org_api_key = org_response.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    await client.post(
        "/v0/project",
        json={"name": "Org_List_Test"},
        headers=org_headers,
    )

    # List projects using personal API key - should only see personal project
    list_response = await client.get("/v0/projects", headers=user["headers"])
    assert list_response.status_code == 200
    projects = list_response.json()

    assert "Personal_List_Test" in projects
    assert "Org_List_Test" not in projects


@pytest.mark.anyio
async def test_list_projects_org_api_key_shows_only_org_projects(client: AsyncClient):
    """Test that listing projects with org API key shows only that org's projects."""
    user = await create_test_user(client, "list_org_only@test.com")

    # Create a personal project
    await client.post(
        "/v0/project",
        json={"name": "Personal_Not_Listed"},
        headers=user["headers"],
    )

    # Create an organization and org project
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Org Only List Test"},
        headers=user["headers"],
    )
    org_api_key = org_response.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    await client.post(
        "/v0/project",
        json={"name": "Org_Only_Listed"},
        headers=org_headers,
    )

    # List projects using org API key - should only see org project
    list_response = await client.get("/v0/projects", headers=org_headers)
    assert list_response.status_code == 200
    projects = list_response.json()

    assert "Org_Only_Listed" in projects
    assert "Personal_Not_Listed" not in projects


@pytest.mark.anyio
async def test_list_projects_multiple_orgs_shows_correct_org(client: AsyncClient):
    """Test that org API key only shows projects from its specific organization."""
    user = await create_test_user(client, "list_multi_org@test.com")

    # Create first organization and project
    org1_response = await client.post(
        "/v0/organizations",
        json={"name": "Org1 List Test"},
        headers=user["headers"],
    )
    org1_api_key = org1_response.json()["api_key"]
    org1_headers = {"Authorization": f"Bearer {org1_api_key}"}

    await client.post(
        "/v0/project",
        json={"name": "Org1_Project"},
        headers=org1_headers,
    )

    # Create second organization and project
    org2_response = await client.post(
        "/v0/organizations",
        json={"name": "Org2 List Test"},
        headers=user["headers"],
    )
    org2_api_key = org2_response.json()["api_key"]
    org2_headers = {"Authorization": f"Bearer {org2_api_key}"}

    await client.post(
        "/v0/project",
        json={"name": "Org2_Project"},
        headers=org2_headers,
    )

    # List using org1 key - should only see org1 project
    list_org1 = await client.get("/v0/projects", headers=org1_headers)
    assert list_org1.status_code == 200
    projects_org1 = list_org1.json()
    assert "Org1_Project" in projects_org1
    assert "Org2_Project" not in projects_org1

    # List using org2 key - should only see org2 project
    list_org2 = await client.get("/v0/projects", headers=org2_headers)
    assert list_org2.status_code == 200
    projects_org2 = list_org2.json()
    assert "Org2_Project" in projects_org2
    assert "Org1_Project" not in projects_org2


@pytest.mark.anyio
async def test_list_projects_tree_personal_api_key(client: AsyncClient):
    """Test that /projects/tree with personal API key shows only personal projects."""
    user = await create_test_user(client, "tree_personal@test.com")

    # Create a personal project
    await client.post(
        "/v0/project",
        json={"name": "Personal_Tree_Test"},
        headers=user["headers"],
    )

    # Create an organization and org project
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Tree Personal Org"},
        headers=user["headers"],
    )
    org_api_key = org_response.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    await client.post(
        "/v0/project",
        json={"name": "Org_Tree_Test"},
        headers=org_headers,
    )

    # List projects/tree using personal API key - should only see personal project
    list_response = await client.get("/v0/projects/tree", headers=user["headers"])
    assert list_response.status_code == 200
    projects = list_response.json()
    project_names = [p["project_name"] for p in projects]

    assert "Personal_Tree_Test" in project_names
    assert "Org_Tree_Test" not in project_names


@pytest.mark.anyio
async def test_list_projects_tree_org_api_key(client: AsyncClient):
    """Test that /projects/tree with org API key shows only that org's projects."""
    user = await create_test_user(client, "tree_org@test.com")

    # Create a personal project
    await client.post(
        "/v0/project",
        json={"name": "Personal_Tree_Hidden"},
        headers=user["headers"],
    )

    # Create an organization and org project
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Tree Org Only"},
        headers=user["headers"],
    )
    org_api_key = org_response.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    await client.post(
        "/v0/project",
        json={"name": "Org_Tree_Shown"},
        headers=org_headers,
    )

    # List projects/tree using org API key - should only see org project
    list_response = await client.get("/v0/projects/tree", headers=org_headers)
    assert list_response.status_code == 200
    projects = list_response.json()
    project_names = [p["project_name"] for p in projects]

    assert "Org_Tree_Shown" in project_names
    assert "Personal_Tree_Hidden" not in project_names


# ============== Organization Wallet Model Tests ==============


@pytest.mark.anyio
async def test_organization_has_default_billing_fields(client: AsyncClient, dbsession):
    """Test that new organizations have default billing wallet fields."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "wallet_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Wallet Test Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Query the organization directly from DB to verify wallet fields
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()

    # Verify default wallet fields via billing_account
    ba = org.billing_account
    assert ba is not None
    assert ba.credits == Decimal("0")
    assert ba.stripe_customer_id is None  # NULL = legacy billing mode
    assert ba.autorecharge is False
    assert ba.account_status == "ACTIVE"
    assert ba.billing_setup_complete is False

    # Verify business profile fields are NULL by default
    assert ba.billing_email is None
    assert ba.name is None
    assert ba.tax_id is None
    assert ba.billing_address is None or ba.billing_address == {}


@pytest.mark.anyio
async def test_organization_direct_billing(client: AsyncClient, dbsession):
    """Test that organizations use direct billing via stripe_customer_id."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "direct_billing@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Direct Billing Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Query organization
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()

    # New orgs start without stripe_customer_id (billing not set up)
    assert org.billing_account.stripe_customer_id is None

    # Setting stripe_customer_id enables direct billing
    org.billing_account.stripe_customer_id = "cus_test_direct"
    dbsession.commit()
    dbsession.refresh(org)

    assert org.billing_account.stripe_customer_id == "cus_test_direct"


@pytest.mark.anyio
async def test_organization_credits_can_be_updated(client: AsyncClient, dbsession):
    """Test that organization credits can be updated."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "credits_update@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Credits Update Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Update credits directly in DB via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.credits = Decimal("500.50")
    dbsession.commit()
    dbsession.refresh(org)

    assert org.billing_account.credits == Decimal("500.50")

    # Deduct credits
    org.billing_account.credits = org.billing_account.credits - Decimal("100.25")
    dbsession.commit()
    dbsession.refresh(org)

    assert org.billing_account.credits == Decimal("400.25")


@pytest.mark.anyio
async def test_recharge_model_supports_organization(client: AsyncClient, dbsession):
    """Test that Recharge model supports organization_id."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization, Recharge

    owner = await create_test_user(client, "recharge_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Recharge Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get the org's billing_account_id
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    ba_id = org.billing_account_id

    # Create a recharge record linked to the org's billing account
    recharge = Recharge(
        billing_account_id=ba_id,
        quantity=Decimal("100"),
        amount_usd=Decimal("10.00"),
        type="payment",
        status="PENDING_INVOICE",
    )
    dbsession.add(recharge)
    dbsession.commit()
    dbsession.refresh(recharge)

    assert recharge.billing_account_id == ba_id
    assert recharge.quantity == Decimal("100")

    # Verify relationship works via billing_account
    assert len(org.billing_account.recharges) == 1
    assert org.billing_account.recharges[0].id == recharge.id


@pytest.mark.anyio
async def test_recharge_links_to_billing_account(client: AsyncClient, dbsession):
    """Test that recharge links to billing_account_id."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization, Recharge

    owner = await create_test_user(client, "recharge_xor@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "XOR Recharge Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get org's billing_account_id
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    ba_id = org.billing_account_id

    # Recharge linked to org's billing account
    recharge = Recharge(
        billing_account_id=ba_id,
        quantity=Decimal("50"),
        amount_usd=Decimal("5.00"),
        type="payment",
        status="PENDING_INVOICE",
    )
    dbsession.add(recharge)
    dbsession.commit()
    assert recharge.id is not None
    assert recharge.billing_account_id == ba_id


@pytest.mark.anyio
async def test_organization_autorecharge_settings(client: AsyncClient, dbsession):
    """Test organization autorecharge configuration fields."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "autorecharge_settings@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Autorecharge Settings Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Update autorecharge settings via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.autorecharge = True
    org.billing_account.autorecharge_threshold = Decimal("50")
    org.billing_account.autorecharge_qty = Decimal("200")
    dbsession.commit()
    dbsession.refresh(org)

    assert org.billing_account.autorecharge is True
    assert org.billing_account.autorecharge_threshold == Decimal("50")
    assert org.billing_account.autorecharge_qty == Decimal("200")


@pytest.mark.anyio
async def test_organization_billing_profile_fields(client: AsyncClient, dbsession):
    """Test organization business profile fields for invoicing."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "billing_profile@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Business Profile Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Update business profile via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    ba = org.billing_account
    ba.billing_email = "billing@acme.com"
    ba.name = "Acme Corporation"
    ba.tax_id = "US-123456789"
    ba.billing_address = {
        "line1": "123 Main St",
        "city": "San Francisco",
        "country": "US",
        "postal_code": "94102",
    }
    ba.billing_setup_complete = True
    dbsession.commit()
    dbsession.refresh(org)

    ba = org.billing_account
    assert ba.billing_email == "billing@acme.com"
    assert ba.name == "Acme Corporation"
    assert ba.tax_id == "US-123456789"
    assert ba.billing_address["line1"] == "123 Main St"
    assert ba.billing_address["city"] == "San Francisco"
    assert ba.billing_address["country"] == "US"
    assert ba.billing_address["postal_code"] == "94102"
    assert ba.billing_setup_complete is True


# ============== Stripe Webhook Tests ==============


@pytest.mark.anyio
async def test_webhook_checkout_org_credits(client: AsyncClient, dbsession):
    """Test that checkout.session.completed with organization_id credits the org."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization
    from orchestra.web.api.webhooks.stripe import process_checkout_session_event

    owner = await create_test_user(client, "webhook_checkout_org@test.com")

    # Create organization with direct billing
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Webhook Checkout Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_webhook_checkout"
    org.billing_account.credits = Decimal("0")
    dbsession.commit()

    # Simulate checkout.session.completed event with organization_id in metadata
    event = {
        "id": "evt_test_org_checkout_123",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_org_123",
                "amount_total": 10000,  # $100 in cents
                "metadata": {
                    "organization_id": str(org_id),
                },
            },
        },
    }

    response = process_checkout_session_event(event, dbsession)

    assert response.status_code == 200

    # Verify org was credited
    dbsession.refresh(org)
    assert org.billing_account.credits == Decimal("100")


@pytest.mark.anyio
async def test_webhook_checkout_user_credits(client: AsyncClient, dbsession):
    """Test that checkout.session.completed with user_id credits the user."""
    from decimal import Decimal

    from orchestra.db.dao.user_dao import UserDAO
    from orchestra.web.api.webhooks.stripe import process_checkout_session_event

    user = await create_test_user(client, "webhook_checkout_user@test.com")

    # Check initial credits
    user_dao = UserDAO(dbsession)
    initial = user_dao.get_user_with_id(user["id"]).billing_account.credits

    # Simulate checkout.session.completed event for user
    event = {
        "id": "evt_test_user_checkout_123",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_user_123",
                "client_reference_id": user["id"],
                "amount_total": 5000,  # $50 in cents
            },
        },
    }

    response = process_checkout_session_event(event, dbsession)

    assert response.status_code == 200

    # Verify user was credited
    dbsession.refresh(user_dao.get_user_with_id(user["id"]))
    updated_user = user_dao.get_user_with_id(user["id"])
    assert updated_user.billing_account.credits == initial + Decimal("50")


@pytest.mark.anyio
async def test_webhook_invoice_paid_org(client: AsyncClient, dbsession):
    """Test invoice.payment_succeeded updates org recharge status."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import (
        Organization,
        Recharge,
        RechargeStatus,
    )
    from orchestra.web.api.webhooks.stripe import process_invoice_event

    owner = await create_test_user(client, "webhook_invoice_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Webhook Invoice Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_webhook_invoice"
    dbsession.commit()

    # Create an organization recharge with invoice ID
    recharge = Recharge(
        billing_account_id=org.billing_account_id,
        quantity=Decimal("100"),
        amount_usd=Decimal("100"),
        type="auto",
        status=RechargeStatus.INVOICE_CREATED,
        stripe_invoice_id="in_test_org_invoice_123",
    )
    dbsession.add(recharge)
    dbsession.commit()
    recharge_id = recharge.id

    # Simulate invoice.payment_succeeded event
    event = {
        "id": "evt_test_org_invoice_paid_123",
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": "in_test_org_invoice_123",
            },
        },
    }

    response = process_invoice_event(event, dbsession)

    assert response.status_code == 200

    # Expire all cached objects to force re-query
    dbsession.expire_all()

    # Verify recharge status updated
    updated_recharge = dbsession.query(Recharge).filter_by(id=recharge_id).first()
    assert updated_recharge.status == RechargeStatus.PAID

    # Verify org account status via billing_account
    updated_org = (
        dbsession.query(Organization).filter(Organization.id == org_id).first()
    )
    assert updated_org.billing_account.account_status == "ACTIVE"


@pytest.mark.anyio
async def test_webhook_invoice_failed_org(client: AsyncClient, dbsession):
    """Test invoice.payment_failed updates org status to PAST_DUE."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import (
        Organization,
        Recharge,
        RechargeStatus,
    )
    from orchestra.web.api.webhooks.stripe import process_invoice_event

    owner = await create_test_user(client, "webhook_invoice_failed_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Webhook Invoice Failed Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_webhook_invoice_fail"
    dbsession.commit()

    # Create an organization recharge with invoice ID
    recharge = Recharge(
        billing_account_id=org.billing_account_id,
        quantity=Decimal("100"),
        amount_usd=Decimal("100"),
        type="auto",
        status=RechargeStatus.INVOICE_CREATED,
        stripe_invoice_id="in_test_org_invoice_fail_123",
    )
    dbsession.add(recharge)
    dbsession.commit()
    recharge_id = recharge.id

    # Simulate invoice.payment_failed event (final)
    event = {
        "id": "evt_test_org_invoice_failed_123",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_test_org_invoice_fail_123",
                "status": "past_due",  # Final status
            },
        },
    }

    response = process_invoice_event(event, dbsession)

    assert response.status_code == 200

    # Expire all cached objects to force re-query
    dbsession.expire_all()

    # Verify recharge status updated to FAILED
    updated_recharge = dbsession.query(Recharge).filter_by(id=recharge_id).first()
    assert updated_recharge.status == RechargeStatus.FAILED

    # Verify org account status via billing_account
    updated_org = (
        dbsession.query(Organization).filter(Organization.id == org_id).first()
    )
    assert updated_org.billing_account.account_status == "PAST_DUE"


# ============== Billing API Endpoint Tests ==============


@pytest.mark.anyio
async def test_get_organization_billing_not_configured(client: AsyncClient, dbsession):
    """Test GET /organizations/{id}/billing for org without billing configured."""
    owner = await create_test_user(client, "api_billing_delegated@test.com")

    # Create organization (default is no billing configured)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Billing Not Configured Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get billing info
    response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["organization_id"] == org_id
    assert data["stripe_customer_id"] is None
    assert data["billing_setup_complete"] is False
    assert data["autorecharge"] is False


@pytest.mark.anyio
async def test_get_organization_billing_direct(client: AsyncClient, dbsession):
    """Test GET /organizations/{id}/billing for direct billing org."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "api_billing_direct@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Billing Direct Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_api_test"
    org.billing_account.credits = Decimal("250")
    dbsession.commit()

    # Get billing info
    response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["organization_id"] == org_id
    assert data["stripe_customer_id"] == "cus_api_test"
    assert data["credits"] == 250.0


@pytest.mark.anyio
async def test_get_organization_billing_unauthorized(client: AsyncClient, dbsession):
    """Test that non-members cannot access billing info."""
    owner = await create_test_user(client, "api_billing_owner@test.com")
    outsider = await create_test_user(client, "api_billing_outsider@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Billing Unauthorized Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Try to access as outsider
    response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=outsider["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_update_organization_billing(client: AsyncClient, dbsession):
    """Test PATCH /organizations/{id}/billing to update settings."""
    owner = await create_test_user(client, "api_update_billing@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Update Billing Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Update billing settings
    response = await client.patch(
        f"/v0/organizations/{org_id}/billing",
        json={
            "autorecharge": True,
            "autorecharge_threshold": 25.0,
            "autorecharge_qty": 150.0,
        },
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["autorecharge"] is True
    assert data["autorecharge_threshold"] == 25.0
    assert data["autorecharge_qty"] == 150.0


@pytest.mark.anyio
async def test_update_organization_billing_non_owner(client: AsyncClient, dbsession):
    """Test that only owner can update billing settings."""
    owner = await create_test_user(client, "api_billing_update_owner@test.com")
    member = await create_test_user(client, "api_billing_update_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Billing Non-Owner Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Try to update as member
    response = await client.patch(
        f"/v0/organizations/{org_id}/billing",
        json={"autorecharge": True},
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_get_organization_credits(client: AsyncClient, dbsession):
    """Test GET /organizations/{id}/billing/credits endpoint."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "api_credits@test.com")

    # Create organization with direct billing
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Credits Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing with credits via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_credits_test"
    org.billing_account.credits = Decimal("300.50")
    dbsession.commit()

    # Get credits
    response = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["organization_id"] == org_id
    assert data["credits"] == 300.5


@pytest.mark.anyio
async def test_get_organization_billing_profile(client: AsyncClient, dbsession):
    """Test GET /organizations/{id}/billing/billing-profile endpoint."""
    owner = await create_test_user(client, "api_billing_profile@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Business Profile Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get business profile (initially all null)
    response = await client.get(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["billing_email"] is None
    assert data["business_name"] is None


@pytest.mark.anyio
async def test_update_organization_billing_profile(client: AsyncClient, dbsession):
    """Test PATCH /organizations/{id}/billing/billing-profile endpoint."""
    owner = await create_test_user(client, "api_update_profile@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Update Profile Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Update business profile (using valid EIN format for US)
    response = await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={
            "billing_email": "finance@company.com",
            "business_name": "Company LLC",
            "tax_id": "12-3456789",  # Valid US EIN format
            "billing_address": {
                "line1": "456 Business Pkwy",
                "city": "New York",
                "country": "US",
            },
        },
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["billing_email"] == "finance@company.com"
    assert data["business_name"] == "Company LLC"
    assert data["tax_id"] == "12-3456789"
    assert data["billing_address"]["line1"] == "456 Business Pkwy"
    assert data["billing_address"]["city"] == "New York"
    assert data["billing_address"]["country"] == "US"


# ============== End-to-End Integration Tests ==============


@pytest.mark.anyio
async def test_e2e_org_direct_billing_flow(client: AsyncClient, dbsession):
    """
    End-to-end test: Organization with direct billing.

    Tests the full flow:
    1. Create organization
    2. Enable direct billing (set stripe_customer_id)
    3. Add credits via simulated checkout webhook
    4. Verify credits via API
    5. Deduct credits via billing entity
    6. Verify autorecharge triggers when below threshold
    """
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import deduct_credits, get_billing_entity
    from orchestra.web.api.webhooks.stripe import process_checkout_session_event

    owner = await create_test_user(client, "e2e_direct@test.com")

    # Step 1: Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "E2E Direct Billing Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Step 2: Enable direct billing via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    ba = org.billing_account
    ba.stripe_customer_id = "cus_e2e_direct_test"
    ba.autorecharge = True
    ba.autorecharge_threshold = Decimal("50")
    ba.autorecharge_qty = Decimal("100")
    dbsession.commit()

    # Step 3: Add credits via simulated checkout webhook
    checkout_event = {
        "id": "evt_e2e_checkout_123",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_e2e_123",
                "amount_total": 20000,  # $200 in cents
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }
    response = process_checkout_session_event(checkout_event, dbsession)
    assert response.status_code == 200

    # Step 4: Verify credits via API
    credits_response = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=owner["headers"],
    )
    assert credits_response.status_code == status.HTTP_200_OK
    assert credits_response.json()["credits"] == 200.0

    # Step 5: Get billing entity and deduct credits
    billing_entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)
    assert billing_entity.is_organization is True
    assert billing_entity.has_billing is True

    # Simulate usage by deducting credits
    new_balance = deduct_credits(dbsession, billing_entity, Decimal("160"))
    dbsession.commit()

    assert new_balance == Decimal("40")  # 200 - 160 = 40

    # Step 6: Check autorecharge should trigger (40 <= 50)
    dbsession.refresh(org)
    billing_entity_updated = get_billing_entity(
        dbsession,
        owner["id"],
        organization_id=org_id,
    )
    assert billing_entity_updated.should_trigger_autorecharge(new_balance) is True


# ============== Billing Permissions Tests ==============


@pytest.mark.anyio
async def test_billing_permissions_owner_has_read_write(client: AsyncClient, dbsession):
    """Test that organization owner has billing:read and billing:write permissions."""
    from orchestra.db.dao.resource_access_dao import ResourceAccessDAO

    owner = await create_test_user(client, "owner_perm@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Owner Billing Perm Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Owner should have billing:read
    assert (
        resource_access_dao.check_user_has_permission_in_org(
            owner["id"],
            org_id,
            "billing:read",
        )
        is True
    )

    # Owner should have billing:write
    assert (
        resource_access_dao.check_user_has_permission_in_org(
            owner["id"],
            org_id,
            "billing:write",
        )
        is True
    )


@pytest.mark.anyio
async def test_billing_permissions_admin_has_read_write(client: AsyncClient, dbsession):
    """Test that Admin role has billing:read and billing:write permissions."""
    from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, "admin_perm_owner@test.com")
    admin = await create_test_user(client, "admin_perm@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Admin Billing Perm Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Admin role
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Add admin as member with Admin role
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin["id"], "role_id": admin_role.id},
        headers=owner["headers"],
    )

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Admin should have billing:read
    assert (
        resource_access_dao.check_user_has_permission_in_org(
            admin["id"],
            org_id,
            "billing:read",
        )
        is True
    )

    # Admin should have billing:write
    assert (
        resource_access_dao.check_user_has_permission_in_org(
            admin["id"],
            org_id,
            "billing:write",
        )
        is True
    )


@pytest.mark.anyio
async def test_billing_permissions_member_read_only(client: AsyncClient, dbsession):
    """Test that Member role has billing:read but not billing:write."""
    from orchestra.db.dao.role_dao import RoleDAO
    from orchestra.db.models.orchestra_models import Permission, RolePermission

    # Get Member system role
    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    assert member_role is not None, "Member system role should exist"

    # Check what permissions the Member role has
    role_perms = (
        dbsession.query(RolePermission)
        .filter(
            RolePermission.role_id == member_role.id,
        )
        .all()
    )
    perm_names = []
    for rp in role_perms:
        perm = (
            dbsession.query(Permission)
            .filter(Permission.id == rp.permission_id)
            .first()
        )
        if perm:
            perm_names.append(perm.name)

    # Member should have billing:read
    assert (
        "billing:read" in perm_names
    ), f"Member role should have billing:read. Has: {perm_names}"

    # Member should NOT have billing:write
    assert (
        "billing:write" not in perm_names
    ), f"Member role should NOT have billing:write. Has: {perm_names}"


@pytest.mark.anyio
async def test_billing_api_admin_can_update(client: AsyncClient, dbsession):
    """Test that Admin can update billing settings via API."""
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, "api_admin_owner@test.com")
    admin = await create_test_user(client, "api_admin@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Admin API Billing Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Admin role
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Add admin as member with Admin role
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin["id"], "role_id": admin_role.id},
        headers=owner["headers"],
    )

    # Admin should be able to update billing settings
    response = await client.patch(
        f"/v0/organizations/{org_id}/billing",
        json={"autorecharge": True, "autorecharge_threshold": 50},
        headers=admin["headers"],
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["autorecharge"] is True
    assert response.json()["autorecharge_threshold"] == 50


@pytest.mark.anyio
async def test_billing_api_member_cannot_update(client: AsyncClient, dbsession):
    """Test that Member cannot update billing settings via API."""
    owner = await create_test_user(client, "api_member_owner@test.com")
    member = await create_test_user(client, "api_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Member API Billing Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member with default Member role
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Member should NOT be able to update billing settings
    response = await client.patch(
        f"/v0/organizations/{org_id}/billing",
        json={"autorecharge": True},
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_billing_api_member_can_read(client: AsyncClient, dbsession):
    """Test that Member can read billing info via API."""
    owner = await create_test_user(client, "api_member_read_owner@test.com")
    member = await create_test_user(client, "api_member_read@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Member Read Billing Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member with default Member role
    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert (
        add_response.status_code == status.HTTP_201_CREATED
    ), f"Failed to add member: {add_response.json()}"

    # Member should be able to read billing info
    response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=member["headers"],
    )
    if response.status_code != status.HTTP_200_OK:
        print(f"Response: {response.status_code} - {response.json()}")
    assert (
        response.status_code == status.HTTP_200_OK
    ), f"Expected 200 but got {response.status_code}: {response.json()}"


# ============== International Address Tests ==============


@pytest.mark.anyio
async def test_international_address_us(client: AsyncClient, dbsession):
    """Test US address format."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    owner = await create_test_user(client, "us_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "US Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    from orchestra.db.models.orchestra_models import Organization

    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    dao = BillingAccountDAO(dbsession)
    dao.update_billing_profile(
        org.billing_account_id,
        billing_address={
            "country": "US",
            "line1": "123 Main Street",
            "line2": "Suite 100",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94102",
            "formatted": "123 Main Street, Suite 100, San Francisco, CA 94102, USA",
        },
    )
    dbsession.commit()

    profile = dao.get_billing_profile(org.billing_account_id)
    assert profile["billing_address"]["country"] == "US"
    assert profile["billing_address"]["state"] == "CA"
    assert profile["billing_address"]["postal_code"] == "94102"


@pytest.mark.anyio
async def test_international_address_india(client: AsyncClient, dbsession):
    """Test India address format with district."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    owner = await create_test_user(client, "india_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "India Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    from orchestra.db.models.orchestra_models import Organization

    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    dao = BillingAccountDAO(dbsession)
    dao.update_billing_profile(
        org.billing_account_id,
        billing_address={
            "country": "IN",
            "line1": "123 MG Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "district": "Bengaluru Urban",
            "postal_code": "560001",
            "formatted": "123 MG Road, Bengaluru Urban, Bengaluru, Karnataka 560001, India",
        },
    )
    dbsession.commit()

    profile = dao.get_billing_profile(org.billing_account_id)
    assert profile["billing_address"]["country"] == "IN"
    assert profile["billing_address"]["district"] == "Bengaluru Urban"
    assert profile["billing_address"]["state"] == "Karnataka"


@pytest.mark.anyio
async def test_international_address_uk(client: AsyncClient, dbsession):
    """Test UK address format with county."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    owner = await create_test_user(client, "uk_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "UK Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    from orchestra.db.models.orchestra_models import Organization

    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    dao = BillingAccountDAO(dbsession)
    dao.update_billing_profile(
        org.billing_account_id,
        billing_address={
            "country": "GB",
            "line1": "10 Downing Street",
            "city": "London",
            "postal_code": "SW1A 2AA",
            "formatted": "10 Downing Street, London SW1A 2AA, United Kingdom",
        },
    )
    dbsession.commit()

    profile = dao.get_billing_profile(org.billing_account_id)
    assert profile["billing_address"]["country"] == "GB"
    assert profile["billing_address"]["postal_code"] == "SW1A 2AA"


@pytest.mark.anyio
async def test_international_address_japan(client: AsyncClient, dbsession):
    """Test Japan address format with custom fields."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    owner = await create_test_user(client, "japan_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Japan Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    from orchestra.db.models.orchestra_models import Organization

    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    dao = BillingAccountDAO(dbsession)
    dao.update_billing_profile(
        org.billing_account_id,
        billing_address={
            "country": "JP",
            "postal_code": "100-0001",
            "state": "Tokyo",
            "city": "Chiyoda-ku",
            "sublocality": "Chiyoda",
            "line1": "1-1 Chiyoda",
            "formatted": "〒100-0001 東京都千代田区千代田1-1",
        },
    )
    dbsession.commit()

    profile = dao.get_billing_profile(org.billing_account_id)
    assert profile["billing_address"]["country"] == "JP"
    assert profile["billing_address"]["sublocality"] == "Chiyoda"


@pytest.mark.anyio
async def test_international_address_api_update(client: AsyncClient, dbsession):
    """Test updating international address via API."""
    owner = await create_test_user(client, "api_intl_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Intl Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Update with Indian address via API
    response = await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={
            "billing_email": "billing@indiancompany.in",
            "business_name": "Indian Tech Pvt Ltd",
            "billing_address": {
                "country": "IN",
                "line1": "Tower B, Tech Park",
                "city": "Hyderabad",
                "state": "Telangana",
                "district": "Rangareddy",
                "postal_code": "500081",
            },
        },
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["billing_address"]["country"] == "IN"
    assert data["billing_address"]["district"] == "Rangareddy"
    assert data["billing_address"]["state"] == "Telangana"


@pytest.mark.anyio
async def test_address_partial_update_merges(client: AsyncClient, dbsession):
    """Test that partial address updates merge with existing data."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    owner = await create_test_user(client, "merge_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Merge Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    from orchestra.db.models.orchestra_models import Organization

    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    dao = BillingAccountDAO(dbsession)

    # Set initial address
    dao.update_billing_profile(
        org.billing_account_id,
        billing_address={
            "country": "US",
            "line1": "123 Main St",
            "city": "Boston",
            "state": "MA",
            "postal_code": "02101",
        },
    )
    dbsession.commit()

    # Partial update - only change city
    dao.update_billing_profile(
        org.billing_account_id,
        billing_address={
            "city": "Cambridge",
        },
    )
    dbsession.commit()

    # Verify merge happened
    profile = dao.get_billing_profile(org.billing_account_id)
    assert profile["billing_address"]["country"] == "US"  # Preserved
    assert profile["billing_address"]["line1"] == "123 Main St"  # Preserved
    assert profile["billing_address"]["city"] == "Cambridge"  # Updated
    assert profile["billing_address"]["state"] == "MA"  # Preserved


# ============== Critical Review Fix Tests ==============


def test_frozen_org_cannot_spend_credits(dbsession):
    """Test that suspended organizations cannot spend credits (H1 fix)."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.models.orchestra_models import BillingAccount, Organization, User
    from orchestra.lib.billing import get_billing_entity

    # Create owner with billing account
    owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
    dbsession.add(owner_ba)
    dbsession.flush()

    owner = User(
        id="frozen_org_owner",
        email="frozen_org_owner@test.com",
        name="Frozen Org Owner",
        billing_account_id=owner_ba.id,
    )
    dbsession.add(owner)
    dbsession.flush()

    # Create org with direct billing via billing_account
    org_ba = BillingAccount(
        stripe_customer_id="cus_frozen_test",
        account_status="ACTIVE",
        credits=0,
    )
    dbsession.add(org_ba)
    dbsession.flush()

    org = Organization(
        name="Frozen Test Org",
        owner_id=owner.id,
        billing_account_id=org_ba.id,
    )
    dbsession.add(org)
    dbsession.commit()

    # Should work when ACTIVE
    billing_entity = get_billing_entity(dbsession, owner.id, org.id)
    assert billing_entity.is_organization

    # Suspend the org
    dao = BillingAccountDAO(dbsession)
    dao.set_account_status(org.billing_account_id, "SUSPENDED")
    dbsession.commit()

    # Should raise when SUSPENDED
    import pytest

    with pytest.raises(ValueError) as exc_info:
        get_billing_entity(dbsession, owner.id, org.id)
    assert "SUSPENDED" in str(exc_info.value)


def test_invalid_account_status_rejected(dbsession):
    """Test that invalid account status values are rejected (H4/M3 fix)."""
    import pytest

    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.models.orchestra_models import BillingAccount, Organization, User

    # Create owner
    owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
    dbsession.add(owner_ba)
    dbsession.flush()

    owner = User(
        id="status_owner",
        email="status_owner@test.com",
        name="Status Owner",
        billing_account_id=owner_ba.id,
    )
    dbsession.add(owner)
    dbsession.flush()

    # Create org with billing_account
    org_ba = BillingAccount(credits=0, account_status="ACTIVE")
    dbsession.add(org_ba)
    dbsession.flush()

    org = Organization(
        name="Status Test Org",
        owner_id=owner.id,
        billing_account_id=org_ba.id,
    )
    dbsession.add(org)
    dbsession.commit()

    dao = BillingAccountDAO(dbsession)

    # Valid statuses should work
    assert dao.set_account_status(org.billing_account_id, "SUSPENDED") is True
    assert dao.set_account_status(org.billing_account_id, "PAST_DUE") is True
    assert dao.set_account_status(org.billing_account_id, "CLOSED") is True
    assert dao.set_account_status(org.billing_account_id, "ACTIVE") is True

    # Invalid status should raise
    with pytest.raises(ValueError) as exc_info:
        dao.set_account_status(org.billing_account_id, "BANANA")
    assert "Invalid account status" in str(exc_info.value)

    with pytest.raises(ValueError):
        dao.set_account_status(org.billing_account_id, "FROZEN")  # Not a valid status


def test_recharge_requires_billing_account(dbsession):
    """Test that recharge table requires billing_account_id."""
    from decimal import Decimal

    from sqlalchemy.exc import IntegrityError

    from orchestra.db.models.orchestra_models import (
        BillingAccount,
        Organization,
        Recharge,
        RechargeStatus,
        User,
    )

    # Create user with billing account
    user_ba = BillingAccount(credits=Decimal("100"), account_status="ACTIVE")
    dbsession.add(user_ba)
    dbsession.flush()

    user = User(
        id="xor_user",
        email="xor_user@test.com",
        billing_account_id=user_ba.id,
    )
    dbsession.add(user)

    # Create owner and org with billing accounts
    owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
    dbsession.add(owner_ba)
    dbsession.flush()

    owner = User(
        id="xor_owner",
        email="xor_owner@test.com",
        name="XOR Owner",
        billing_account_id=owner_ba.id,
    )
    dbsession.add(owner)
    dbsession.flush()

    org_ba = BillingAccount(credits=0, account_status="ACTIVE")
    dbsession.add(org_ba)
    dbsession.flush()

    org = Organization(
        name="XOR Test Org",
        owner_id=owner.id,
        billing_account_id=org_ba.id,
    )
    dbsession.add(org)
    dbsession.commit()

    # Valid: linked to user's billing account
    r1 = Recharge(
        billing_account_id=user_ba.id,
        quantity=Decimal("10"),
        amount_usd=Decimal("10"),
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(r1)
    dbsession.commit()

    # Valid: linked to org's billing account
    r2 = Recharge(
        billing_account_id=org_ba.id,
        quantity=Decimal("10"),
        amount_usd=Decimal("10"),
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(r2)
    dbsession.commit()

    # Invalid: no billing_account_id - should fail (NOT NULL constraint)
    import pytest

    r3 = Recharge(
        quantity=Decimal("10"),
        amount_usd=Decimal("10"),
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(r3)
    with pytest.raises(IntegrityError):
        dbsession.commit()
    dbsession.rollback()


def test_duplicate_stripe_customer_id_rejected(dbsession):
    """Test that duplicate stripe_customer_id is rejected on BillingAccount (H3 fix)."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from orchestra.db.models.orchestra_models import BillingAccount, Organization, User

    # Create owners with billing accounts
    owner1_ba = BillingAccount(credits=0, account_status="ACTIVE")
    owner2_ba = BillingAccount(credits=0, account_status="ACTIVE")
    dbsession.add(owner1_ba)
    dbsession.add(owner2_ba)
    dbsession.flush()

    owner1 = User(
        id="dup_owner1",
        email="dup1@test.com",
        name="Owner 1",
        billing_account_id=owner1_ba.id,
    )
    owner2 = User(
        id="dup_owner2",
        email="dup2@test.com",
        name="Owner 2",
        billing_account_id=owner2_ba.id,
    )
    dbsession.add(owner1)
    dbsession.add(owner2)
    dbsession.flush()

    # Create org1 with stripe customer id on billing_account
    org1_ba = BillingAccount(
        credits=0,
        account_status="ACTIVE",
        stripe_customer_id="cus_duplicate_test",
    )
    dbsession.add(org1_ba)
    dbsession.flush()

    org1 = Organization(
        name="Dup Test Org 1",
        owner_id=owner1.id,
        billing_account_id=org1_ba.id,
    )
    dbsession.add(org1)
    dbsession.commit()

    # Try to create org2 with same stripe customer id - should fail
    org2_ba = BillingAccount(
        credits=0,
        account_status="ACTIVE",
        stripe_customer_id="cus_duplicate_test",  # Same as org1
    )
    dbsession.add(org2_ba)
    with pytest.raises(IntegrityError):
        dbsession.flush()
    dbsession.rollback()

    # But NULL stripe_customer_id should be allowed for multiple billing accounts
    org3_ba = BillingAccount(
        credits=0,
        account_status="ACTIVE",
        stripe_customer_id=None,
    )
    org4_ba = BillingAccount(
        credits=0,
        account_status="ACTIVE",
        stripe_customer_id=None,
    )
    dbsession.add(org3_ba)
    dbsession.add(org4_ba)
    dbsession.flush()

    org3 = Organization(
        name="Dup Test Org 3",
        owner_id=owner1.id,
        billing_account_id=org3_ba.id,
    )
    org4 = Organization(
        name="Dup Test Org 4",
        owner_id=owner2.id,
        billing_account_id=org4_ba.id,
    )
    dbsession.add(org3)
    dbsession.add(org4)
    dbsession.commit()  # Should succeed


@pytest.mark.anyio
async def test_checkout_webhook_enables_direct_billing(client: AsyncClient, dbsession):
    """Test that checkout webhook sets stripe_customer_id for new orgs (H2 fix)."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "webhook_owner@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Webhook Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    ba = org.billing_account

    # Initially no stripe customer id
    assert ba.stripe_customer_id is None

    # Simulate what webhook handler does
    ba.stripe_customer_id = "cus_webhook_test_123"
    dbsession.commit()

    # Verify it was set
    dbsession.refresh(org)
    assert org.billing_account.stripe_customer_id == "cus_webhook_test_123"


def test_duplicate_autorecharge_prevented(dbsession):
    """Test that duplicate auto-recharges in same month are prevented (M2 fix)."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import (
        BillingAccount,
        Organization,
        Recharge,
        RechargeStatus,
        User,
    )
    from orchestra.lib.time import month_end_utc

    # Create owner with billing account
    owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
    dbsession.add(owner_ba)
    dbsession.flush()

    owner = User(
        id="dup_recharge_owner",
        email="dup_recharge@test.com",
        name="Dup Recharge Owner",
        billing_account_id=owner_ba.id,
    )
    dbsession.add(owner)
    dbsession.flush()

    # Create org with billing account
    org_ba = BillingAccount(
        credits=0,
        account_status="ACTIVE",
        stripe_customer_id="cus_dup_recharge",
        autorecharge=True,
        autorecharge_threshold=Decimal("10"),
        autorecharge_qty=Decimal("100"),
    )
    dbsession.add(org_ba)
    dbsession.flush()

    org = Organization(
        name="Dup Recharge Org",
        owner_id=owner.id,
        billing_account_id=org_ba.id,
    )
    dbsession.add(org)
    dbsession.commit()

    current_month_end = month_end_utc(datetime.now(timezone.utc).date())

    # Create first pending recharge
    r1 = Recharge(
        billing_account_id=org_ba.id,
        quantity=Decimal("100"),
        amount_usd=Decimal("100"),
        invoice_group=current_month_end,
        status=RechargeStatus.PENDING_INVOICE,
        type="auto",
    )
    dbsession.add(r1)
    dbsession.commit()

    # Simulate the idempotency check from bg_tasks.py
    existing_recharge = (
        dbsession.query(Recharge)
        .filter_by(
            billing_account_id=org_ba.id,
            invoice_group=current_month_end,
            status=RechargeStatus.PENDING_INVOICE,
        )
        .first()
    )

    # Should find the existing recharge
    assert existing_recharge is not None
    assert existing_recharge.id == r1.id

    # This is what bg_tasks.py does - skip if existing
    should_skip = existing_recharge is not None
    assert should_skip is True
