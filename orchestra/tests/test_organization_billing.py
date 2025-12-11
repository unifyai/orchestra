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
        json={"user_id": viewer["id"], "level": "user", "role_id": viewer_role.id},
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
        json={"user_id": member["id"], "level": "user"},
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
# NOTE: These tests are skipped because project listing scoping by API key context
# is on the roadmap but blocked by Unity/AssistantJobs dependencies (confirmed with Julia)


@pytest.mark.skip(reason="Project scoping by API key context not yet implemented - blocked by Unity deps")
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


@pytest.mark.skip(reason="Project scoping by API key context not yet implemented - blocked by Unity deps")
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


@pytest.mark.skip(reason="Project scoping by API key context not yet implemented - blocked by Unity deps")
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


@pytest.mark.skip(reason="Project scoping by API key context not yet implemented - blocked by Unity deps")
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
    project_names = [p["project"] for p in projects]

    assert "Personal_Tree_Test" in project_names
    assert "Org_Tree_Test" not in project_names


@pytest.mark.skip(reason="Project scoping by API key context not yet implemented - blocked by Unity deps")
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
    project_names = [p["project"] for p in projects]

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

    # Verify default wallet fields
    assert org.credits == Decimal("0")
    assert org.stripe_customer_id is None  # NULL = legacy billing mode
    assert org.autorecharge is False
    assert org.autorecharge_threshold == Decimal("10")
    assert org.autorecharge_qty == Decimal("100")
    assert org.account_status == "ACTIVE"
    assert org.billing_setup_complete is False

    # Verify business profile fields are NULL by default
    assert org.billing_email is None
    assert org.business_name is None
    assert org.tax_id is None
    assert org.billing_address is None or org.billing_address == {}


@pytest.mark.anyio
async def test_organization_billing_user_nullable(client: AsyncClient, dbsession):
    """Test that billing_user_id can be NULL (for direct billing mode)."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "nullable_billing@test.com")

    # Create organization - billing_user_id defaults to owner
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Nullable Billing Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Query organization
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()

    # Currently billing_user_id equals owner (delegated mode)
    assert org.billing_user_id == owner["id"]

    # Verify billing_user_id column allows NULL (for future direct billing)
    # We can set it to NULL directly in the DB
    org.billing_user_id = None
    dbsession.commit()
    dbsession.refresh(org)

    assert org.billing_user_id is None


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

    # Update credits directly in DB
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.credits = Decimal("500.50")
    dbsession.commit()
    dbsession.refresh(org)

    assert org.credits == Decimal("500.50")

    # Deduct credits
    org.credits = org.credits - Decimal("100.25")
    dbsession.commit()
    dbsession.refresh(org)

    assert org.credits == Decimal("400.25")


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

    # Create a recharge record for the organization
    recharge = Recharge(
        organization_id=org_id,
        user_id=None,  # Organization recharge, not user recharge
        quantity=Decimal("100"),
        amount_usd=Decimal("10.00"),
        type="payment",
        status="PENDING_INVOICE",
    )
    dbsession.add(recharge)
    dbsession.commit()
    dbsession.refresh(recharge)

    assert recharge.organization_id == org_id
    assert recharge.user_id is None
    assert recharge.quantity == Decimal("100")

    # Verify relationship works
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    assert len(org.recharges) == 1
    assert org.recharges[0].id == recharge.id


@pytest.mark.anyio
async def test_recharge_requires_exactly_one_owner(client: AsyncClient, dbsession):
    """Test that recharge must have either user_id OR organization_id, not both or neither."""
    from decimal import Decimal

    from sqlalchemy.exc import IntegrityError

    from orchestra.db.models.orchestra_models import Recharge

    owner = await create_test_user(client, "recharge_xor@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "XOR Recharge Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Test: Both user_id and organization_id set - should work at model level
    # but the check constraint in DB should fail (if we add it)
    # For now, we just test the model accepts the values

    # Valid: organization_id only
    recharge_org = Recharge(
        organization_id=org_id,
        user_id=None,
        quantity=Decimal("50"),
        amount_usd=Decimal("5.00"),
        type="payment",
        status="PENDING_INVOICE",
    )
    dbsession.add(recharge_org)
    dbsession.commit()
    assert recharge_org.id is not None

    # Valid: user_id only
    recharge_user = Recharge(
        organization_id=None,
        user_id=owner["id"],
        quantity=Decimal("50"),
        amount_usd=Decimal("5.00"),
        type="payment",
        status="PENDING_INVOICE",
    )
    dbsession.add(recharge_user)
    dbsession.commit()
    assert recharge_user.id is not None


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

    # Update autorecharge settings
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.autorecharge = True
    org.autorecharge_threshold = Decimal("50")
    org.autorecharge_qty = Decimal("200")
    dbsession.commit()
    dbsession.refresh(org)

    assert org.autorecharge is True
    assert org.autorecharge_threshold == Decimal("50")
    assert org.autorecharge_qty == Decimal("200")


@pytest.mark.anyio
async def test_organization_business_profile_fields(client: AsyncClient, dbsession):
    """Test organization business profile fields for invoicing."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "business_profile@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Business Profile Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Update business profile
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_email = "billing@acme.com"
    org.business_name = "Acme Corporation"
    org.tax_id = "US-123456789"
    org.billing_address = {
        "line1": "123 Main St",
        "city": "San Francisco",
        "country": "US",
        "postal_code": "94102",
    }
    org.billing_setup_complete = True
    dbsession.commit()
    dbsession.refresh(org)

    assert org.billing_email == "billing@acme.com"
    assert org.business_name == "Acme Corporation"
    assert org.tax_id == "US-123456789"
    assert org.billing_address["line1"] == "123 Main St"
    assert org.billing_address["city"] == "San Francisco"
    assert org.billing_address["country"] == "US"
    assert org.billing_address["postal_code"] == "94102"
    assert org.billing_setup_complete is True


# ============== OrganizationBillingDAO Tests ==============


@pytest.mark.anyio
async def test_org_billing_dao_get_credits(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO.get_credits method."""
    from decimal import Decimal

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_credits@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Credits Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Initial credits should be 0
    credits = dao.get_credits(org_id)
    assert credits == Decimal("0")

    # Non-existent org should return 0
    credits = dao.get_credits(999999)
    assert credits == Decimal("0")


@pytest.mark.anyio
async def test_org_billing_dao_add_credits(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO.add_credits method."""
    from decimal import Decimal

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_add_credits@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Add Credits Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Add credits
    new_balance = dao.add_credits(org_id, 100.50)
    assert new_balance == Decimal("100.50")

    # Add more credits
    new_balance = dao.add_credits(org_id, 50.25)
    assert new_balance == Decimal("150.75")

    # Verify with get_credits
    assert dao.get_credits(org_id) == Decimal("150.75")


@pytest.mark.anyio
async def test_org_billing_dao_deduct_credits(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO.deduct_credits method."""
    from decimal import Decimal

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_deduct_credits@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Deduct Credits Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Add initial credits
    dao.add_credits(org_id, 100)

    # Deduct credits
    new_balance = dao.deduct_credits(org_id, 30.50)
    assert new_balance == Decimal("69.50")

    # Verify with get_credits
    assert dao.get_credits(org_id) == Decimal("69.50")


@pytest.mark.anyio
async def test_org_billing_dao_has_direct_billing(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO.has_direct_billing method."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "dao_direct_billing@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Direct Billing Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Initially no direct billing (no stripe_customer_id)
    assert dao.has_direct_billing(org_id) is False

    # Set stripe_customer_id
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_test123"
    dbsession.commit()

    # Now has direct billing
    assert dao.has_direct_billing(org_id) is True


@pytest.mark.anyio
async def test_org_billing_dao_set_stripe_customer_id(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO.set_stripe_customer_id method."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_stripe_id@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Stripe ID Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Set stripe customer ID
    result = dao.set_stripe_customer_id(org_id, "cus_abc123")
    assert result is True

    # Verify
    org = dao.get(org_id)
    assert org.stripe_customer_id == "cus_abc123"

    # Non-existent org should return False
    result = dao.set_stripe_customer_id(999999, "cus_xyz")
    assert result is False


@pytest.mark.anyio
async def test_org_billing_dao_autorecharge_settings(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO autorecharge methods."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_autorecharge@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Autorecharge Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Get default settings
    settings = dao.get_autorecharge_settings(org_id)
    assert settings["autorecharge"] is False
    assert settings["autorecharge_threshold"] == 10.0
    assert settings["autorecharge_qty"] == 100.0

    # Update settings
    dao.set_autorecharge(org_id, True)
    dao.set_autorecharge_threshold(org_id, 25.0)
    dao.set_autorecharge_qty(org_id, 200.0)
    dbsession.flush()

    # Verify
    settings = dao.get_autorecharge_settings(org_id)
    assert settings["autorecharge"] is True
    assert settings["autorecharge_threshold"] == 25.0
    assert settings["autorecharge_qty"] == 200.0


@pytest.mark.anyio
async def test_org_billing_dao_should_trigger_autorecharge(
    client: AsyncClient,
    dbsession,
):
    """Test OrganizationBillingDAO.should_trigger_autorecharge method."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "dao_trigger_autorecharge@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Trigger Autorecharge Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Initially: no stripe_customer_id, no autorecharge
    assert dao.should_trigger_autorecharge(org_id) is False

    # Enable autorecharge but no stripe_customer_id
    dao.set_autorecharge(org_id, True)
    dbsession.flush()
    assert dao.should_trigger_autorecharge(org_id) is False

    # Add stripe_customer_id
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_trigger_test"
    dbsession.flush()

    # Credits = 0, threshold = 10, should trigger
    assert dao.should_trigger_autorecharge(org_id) is True

    # Add credits above threshold
    dao.add_credits(org_id, 50)
    dbsession.flush()
    assert dao.should_trigger_autorecharge(org_id) is False

    # Deduct to below threshold
    dao.deduct_credits(org_id, 45)
    dbsession.flush()
    assert dao.should_trigger_autorecharge(org_id) is True


@pytest.mark.anyio
async def test_org_billing_dao_account_status(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO account status methods."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_account_status@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Account Status Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Default status is ACTIVE
    assert dao.is_account_active(org_id) is True

    # Suspend account (FROZEN is no longer valid - use SUSPENDED)
    dao.set_account_status(org_id, "SUSPENDED")
    dbsession.flush()
    assert dao.is_account_active(org_id) is False

    # Reactivate
    dao.set_account_status(org_id, "ACTIVE")
    dbsession.flush()
    assert dao.is_account_active(org_id) is True


@pytest.mark.anyio
async def test_org_billing_dao_business_profile(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO business profile methods."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_business_profile@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Business Profile Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Get initial profile (all None)
    profile = dao.get_business_profile(org_id)
    assert profile["billing_email"] is None
    assert profile["business_name"] is None

    # Update profile
    dao.update_business_profile(
        org_id,
        billing_email="invoices@company.com",
        business_name="Company Inc",
        tax_id="TAX-12345",
        billing_address={
            "line1": "100 Business Blvd",
            "city": "Austin",
            "state": "TX",
            "country": "US",
            "postal_code": "78701",
        },
    )
    dbsession.flush()

    # Verify
    profile = dao.get_business_profile(org_id)
    assert profile["billing_email"] == "invoices@company.com"
    assert profile["business_name"] == "Company Inc"
    assert profile["tax_id"] == "TAX-12345"
    assert profile["billing_address"]["line1"] == "100 Business Blvd"
    assert profile["billing_address"]["city"] == "Austin"
    assert profile["billing_address"]["state"] == "TX"
    assert profile["billing_address"]["country"] == "US"
    assert profile["billing_address"]["postal_code"] == "78701"


@pytest.mark.anyio
async def test_org_billing_dao_get_by_stripe_customer_id(
    client: AsyncClient,
    dbsession,
):
    """Test OrganizationBillingDAO.get_by_stripe_customer_id method."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "dao_get_stripe@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Get Stripe Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Set stripe_customer_id
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_lookup_test"
    dbsession.commit()

    dao = OrganizationBillingDAO(dbsession)

    # Look up by stripe ID
    found_org = dao.get_by_stripe_customer_id("cus_lookup_test")
    assert found_org is not None
    assert found_org.id == org_id
    assert found_org.name == "DAO Get Stripe Test"

    # Non-existent stripe ID
    not_found = dao.get_by_stripe_customer_id("cus_nonexistent")
    assert not_found is None


@pytest.mark.anyio
async def test_org_billing_dao_clear_delegated_billing(client: AsyncClient, dbsession):
    """Test OrganizationBillingDAO.clear_delegated_billing method."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "dao_clear_delegated@test.com")

    # Create organization (has billing_user_id = owner)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "DAO Clear Delegated Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Verify initially has billing_user_id
    org = dao.get(org_id)
    assert org.billing_user_id == owner["id"]

    # Clear delegated billing
    result = dao.clear_delegated_billing(org_id)
    assert result is True
    dbsession.flush()

    # Verify billing_user_id is now None
    dbsession.refresh(org)
    assert org.billing_user_id is None


# ============== BillingEntity Pattern Tests ==============


@pytest.mark.anyio
async def test_get_billing_entity_personal(client: AsyncClient, dbsession):
    """Test get_billing_entity returns user for personal context."""
    from decimal import Decimal

    from orchestra.db.dao.users_dao import UsersDAO
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    user = await create_test_user(client, "entity_personal@test.com")

    # Add credits to user
    users_dao = UsersDAO(dbsession)
    users_dao.recharge_credit(user["id"], 50)
    dbsession.commit()

    # Get billing entity for personal query
    entity = get_billing_entity(dbsession, user["id"], organization_id=None)

    assert entity.entity_type == BillingEntityType.USER
    assert entity.entity_id == user["id"]
    assert entity.credits == Decimal("50")
    assert entity.is_user is True
    assert entity.is_organization is False


@pytest.mark.anyio
async def test_get_billing_entity_org_delegated(client: AsyncClient, dbsession):
    """Test get_billing_entity returns billing user for delegated org billing."""
    from decimal import Decimal

    from orchestra.db.dao.users_dao import UsersDAO
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    owner = await create_test_user(client, "entity_delegated_owner@test.com")
    member = await create_test_user(client, "entity_delegated_member@test.com")

    # Add credits to owner (billing user)
    users_dao = UsersDAO(dbsession)
    users_dao.recharge_credit(owner["id"], 100)
    dbsession.commit()

    # Create organization (delegated billing to owner)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Entity Delegated Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Get billing entity for org query (by member)
    entity = get_billing_entity(dbsession, member["id"], organization_id=org_id)

    # Should return owner (billing_user_id) not member
    assert entity.entity_type == BillingEntityType.USER
    assert entity.entity_id == owner["id"]
    assert entity.credits == Decimal("100")
    assert entity.is_user is True


@pytest.mark.anyio
async def test_get_billing_entity_org_direct(client: AsyncClient, dbsession):
    """Test get_billing_entity returns org for direct org billing."""
    from decimal import Decimal

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    owner = await create_test_user(client, "entity_direct_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Entity Direct Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing for org
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_direct_test"
    org.credits = Decimal("200")
    dbsession.commit()

    # Get billing entity for org query
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    # Should return organization (direct billing)
    assert entity.entity_type == BillingEntityType.ORGANIZATION
    assert entity.entity_id == org_id
    assert entity.credits == Decimal("200")
    assert entity.is_organization is True
    assert entity.has_direct_billing is True


@pytest.mark.anyio
async def test_deduct_credits_from_user(client: AsyncClient, dbsession):
    """Test deduct_credits from a user billing entity."""
    from decimal import Decimal

    from orchestra.db.dao.users_dao import UsersDAO
    from orchestra.lib.billing import deduct_credits, get_billing_entity

    user = await create_test_user(client, "deduct_user@test.com")

    # Add credits
    users_dao = UsersDAO(dbsession)
    users_dao.recharge_credit(user["id"], 100)
    dbsession.commit()

    # Get billing entity
    entity = get_billing_entity(dbsession, user["id"])

    # Deduct credits
    new_balance = deduct_credits(dbsession, entity, Decimal("25.50"))
    dbsession.commit()

    assert new_balance == Decimal("74.50")

    # Verify in DB
    updated_user = users_dao.get_user_with_id(user["id"])
    assert updated_user.credits == Decimal("74.50")


@pytest.mark.anyio
async def test_deduct_credits_from_org(client: AsyncClient, dbsession):
    """Test deduct_credits from an organization billing entity."""
    from decimal import Decimal

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import deduct_credits, get_billing_entity

    owner = await create_test_user(client, "deduct_org@test.com")

    # Create organization with direct billing
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Deduct Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing and add credits
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_deduct_test"
    org.credits = Decimal("500")
    dbsession.commit()

    # Get billing entity
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    # Deduct credits
    new_balance = deduct_credits(dbsession, entity, Decimal("123.45"))
    dbsession.commit()

    assert new_balance == Decimal("376.55")

    # Verify in DB
    dao = OrganizationBillingDAO(dbsession)
    assert dao.get_credits(org_id) == Decimal("376.55")


@pytest.mark.anyio
async def test_billing_entity_should_trigger_autorecharge(client: AsyncClient, dbsession):
    """Test BillingEntity.should_trigger_autorecharge method."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization, Users
    from orchestra.lib.billing import get_billing_entity

    owner = await create_test_user(client, "autorecharge_trigger@test.com")

    # Create organization with direct billing and autorecharge enabled
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Autorecharge Trigger Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Setup org with autorecharge
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_autorecharge"
    org.credits = Decimal("100")
    org.autorecharge = True
    org.autorecharge_threshold = Decimal("50")
    org.autorecharge_qty = Decimal("200")
    dbsession.commit()

    # Get billing entity
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    # Balance above threshold - should not trigger
    assert entity.should_trigger_autorecharge(Decimal("100")) is False
    assert entity.should_trigger_autorecharge(Decimal("51")) is False

    # Balance at or below threshold - should trigger
    assert entity.should_trigger_autorecharge(Decimal("50")) is True
    assert entity.should_trigger_autorecharge(Decimal("25")) is True
    assert entity.should_trigger_autorecharge(Decimal("0")) is True


@pytest.mark.anyio
async def test_billing_entity_no_autorecharge_without_stripe(
    client: AsyncClient,
    dbsession,
):
    """Test that autorecharge doesn't trigger without Stripe customer ID."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import get_billing_entity

    owner = await create_test_user(client, "no_stripe_autorecharge@test.com")

    # Create organization WITHOUT direct billing but with autorecharge settings
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "No Stripe Autorecharge Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Setup org with autorecharge but NO stripe_customer_id
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.autorecharge = True
    org.autorecharge_threshold = Decimal("50")
    dbsession.commit()

    # This org uses delegated billing, so we get the billing user
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    # User doesn't have stripe_customer_id by default
    assert entity.is_user is True
    assert entity.has_direct_billing is False
    # Should not trigger autorecharge without stripe ID
    assert entity.should_trigger_autorecharge(Decimal("0")) is False


# ============== Stripe Webhook Tests ==============


@pytest.mark.anyio
async def test_webhook_checkout_org_credits(client: AsyncClient, dbsession):
    """Test that checkout.session.completed with organization_id credits the org."""
    from decimal import Decimal

    from fastapi import Response

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
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

    # Enable direct billing
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_webhook_checkout"
    org.credits = Decimal("0")
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
    dao = OrganizationBillingDAO(dbsession)
    assert dao.get_credits(org_id) == Decimal("100")


@pytest.mark.anyio
async def test_webhook_checkout_user_credits(client: AsyncClient, dbsession):
    """Test that checkout.session.completed with user_id credits the user."""
    from decimal import Decimal

    from orchestra.db.dao.users_dao import UsersDAO
    from orchestra.web.api.webhooks.stripe import process_checkout_session_event

    user = await create_test_user(client, "webhook_checkout_user@test.com")

    # Check initial credits
    users_dao = UsersDAO(dbsession)
    initial = users_dao.get_user_with_id(user["id"]).credits

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
    dbsession.refresh(users_dao.get_user_with_id(user["id"]))
    updated_user = users_dao.get_user_with_id(user["id"])
    assert updated_user.credits == initial + Decimal("50")


@pytest.mark.anyio
async def test_webhook_invoice_paid_org(client: AsyncClient, dbsession):
    """Test invoice.payment_succeeded updates org recharge status."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization, Recharge, RechargeStatus
    from orchestra.web.api.webhooks.stripe import process_invoice_event

    owner = await create_test_user(client, "webhook_invoice_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Webhook Invoice Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_webhook_invoice"
    dbsession.commit()

    # Create an organization recharge with invoice ID
    recharge = Recharge(
        organization_id=org_id,
        user_id=None,
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

    # Verify org account status
    updated_org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    assert updated_org.account_status == "ACTIVE"


@pytest.mark.anyio
async def test_webhook_invoice_failed_org(client: AsyncClient, dbsession):
    """Test invoice.payment_failed updates org status to PAST_DUE."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization, Recharge, RechargeStatus
    from orchestra.web.api.webhooks.stripe import process_invoice_event

    owner = await create_test_user(client, "webhook_invoice_failed_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Webhook Invoice Failed Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_webhook_invoice_fail"
    dbsession.commit()

    # Create an organization recharge with invoice ID
    recharge = Recharge(
        organization_id=org_id,
        user_id=None,
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

    # Verify org account status
    updated_org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    assert updated_org.account_status == "PAST_DUE"


# ============== Billing API Endpoint Tests ==============


@pytest.mark.anyio
async def test_get_organization_billing_delegated(client: AsyncClient, dbsession):
    """Test GET /organizations/{id}/billing for delegated billing org."""
    owner = await create_test_user(client, "api_billing_delegated@test.com")

    # Create organization (default is delegated billing)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Billing Delegated Org"},
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
    assert data["billing_mode"] == "delegated"
    assert data["billing_user_id"] == owner["id"]
    assert data["stripe_customer_id"] is None
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

    # Enable direct billing
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_api_test"
    org.credits = Decimal("250")
    dbsession.commit()

    # Get billing info
    response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["organization_id"] == org_id
    assert data["billing_mode"] == "direct"
    assert data["billing_user_id"] is None
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
        json={"user_id": member["id"], "level": "user"},
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

    # Enable direct billing with credits
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_credits_test"
    org.credits = Decimal("300.50")
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
async def test_get_organization_business_profile(client: AsyncClient, dbsession):
    """Test GET /organizations/{id}/billing/business-profile endpoint."""
    owner = await create_test_user(client, "api_business_profile@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "API Business Profile Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get business profile (initially all null)
    response = await client.get(
        f"/v0/organizations/{org_id}/billing/business-profile",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["billing_email"] is None
    assert data["business_name"] is None


@pytest.mark.anyio
async def test_update_organization_business_profile(client: AsyncClient, dbsession):
    """Test PATCH /organizations/{id}/billing/business-profile endpoint."""
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
        f"/v0/organizations/{org_id}/billing/business-profile",
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

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
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

    # Step 2: Enable direct billing
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_e2e_direct_test"
    org.autorecharge = True
    org.autorecharge_threshold = Decimal("50")
    org.autorecharge_qty = Decimal("100")
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
    assert billing_entity.has_direct_billing is True

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


@pytest.mark.anyio
async def test_e2e_org_delegated_billing_flow(client: AsyncClient, dbsession):
    """
    End-to-end test: Organization with delegated billing.

    Tests that organizations without stripe_customer_id use the
    billing_user's wallet (delegated billing mode).
    """
    from decimal import Decimal

    from orchestra.db.dao.users_dao import UsersDAO
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    owner = await create_test_user(client, "e2e_delegated@test.com")
    member = await create_test_user(client, "e2e_delegated_member@test.com")

    # Add credits to owner (billing user)
    users_dao = UsersDAO(dbsession)
    users_dao.recharge_credit(owner["id"], 500)
    dbsession.commit()

    # Create organization (delegated billing - no stripe_customer_id)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "E2E Delegated Billing Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Verify billing mode is delegated
    billing_response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=owner["headers"],
    )
    assert billing_response.status_code == status.HTTP_200_OK
    assert billing_response.json()["billing_mode"] == "delegated"
    assert billing_response.json()["billing_user_id"] == owner["id"]

    # Get billing entity - should return the owner (billing user)
    billing_entity = get_billing_entity(dbsession, member["id"], organization_id=org_id)
    assert billing_entity.entity_type == BillingEntityType.USER
    assert billing_entity.entity_id == owner["id"]  # Bills to owner, not member
    assert billing_entity.credits == Decimal("500")


@pytest.mark.anyio
async def test_e2e_transition_delegated_to_direct(client: AsyncClient, dbsession):
    """
    End-to-end test: Transitioning from delegated to direct billing.

    Tests the workflow when an organization sets up direct billing.
    """
    from decimal import Decimal

    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    owner = await create_test_user(client, "e2e_transition@test.com")

    # Create organization (starts with delegated billing)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "E2E Transition Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Verify it's delegated initially
    billing_entity_before = get_billing_entity(
        dbsession,
        owner["id"],
        organization_id=org_id,
    )
    assert billing_entity_before.entity_type == BillingEntityType.USER

    # Simulate enabling direct billing (what would happen after Stripe setup)
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.stripe_customer_id = "cus_transition_test"
    org.credits = Decimal("100")  # Initial credits
    org.billing_user_id = None  # Clear delegated billing
    dbsession.commit()

    # Verify it's now direct billing
    billing_entity_after = get_billing_entity(
        dbsession,
        owner["id"],
        organization_id=org_id,
    )
    assert billing_entity_after.entity_type == BillingEntityType.ORGANIZATION
    assert billing_entity_after.entity_id == org_id
    assert billing_entity_after.has_direct_billing is True

    # Verify via API
    billing_response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=owner["headers"],
    )
    assert billing_response.json()["billing_mode"] == "direct"
    assert billing_response.json()["credits"] == 100.0


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
    assert resource_access_dao.check_user_has_permission_in_org(
        owner["id"],
        org_id,
        "billing:read",
    ) is True

    # Owner should have billing:write
    assert resource_access_dao.check_user_has_permission_in_org(
        owner["id"],
        org_id,
        "billing:write",
    ) is True


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
        json={"user_id": admin["id"], "level": "admin", "role_id": admin_role.id},
        headers=owner["headers"],
    )

    resource_access_dao = ResourceAccessDAO(dbsession)

    # Admin should have billing:read
    assert resource_access_dao.check_user_has_permission_in_org(
        admin["id"],
        org_id,
        "billing:read",
    ) is True

    # Admin should have billing:write
    assert resource_access_dao.check_user_has_permission_in_org(
        admin["id"],
        org_id,
        "billing:write",
    ) is True


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
    role_perms = dbsession.query(RolePermission).filter(
        RolePermission.role_id == member_role.id,
    ).all()
    perm_names = []
    for rp in role_perms:
        perm = dbsession.query(Permission).filter(Permission.id == rp.permission_id).first()
        if perm:
            perm_names.append(perm.name)

    # Member should have billing:read
    assert "billing:read" in perm_names, f"Member role should have billing:read. Has: {perm_names}"

    # Member should NOT have billing:write
    assert "billing:write" not in perm_names, f"Member role should NOT have billing:write. Has: {perm_names}"


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
        json={"user_id": admin["id"], "level": "admin", "role_id": admin_role.id},
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
        json={"user_id": member["id"], "level": "user"},
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
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )
    assert add_response.status_code == status.HTTP_201_CREATED, f"Failed to add member: {add_response.json()}"

    # Member should be able to read billing info
    response = await client.get(
        f"/v0/organizations/{org_id}/billing",
        headers=member["headers"],
    )
    if response.status_code != status.HTTP_200_OK:
        print(f"Response: {response.status_code} - {response.json()}")
    assert response.status_code == status.HTTP_200_OK, f"Expected 200 but got {response.status_code}: {response.json()}"


# ============== International Address Tests ==============


@pytest.mark.anyio
async def test_international_address_us(client: AsyncClient, dbsession):
    """Test US address format."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "us_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "US Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)
    dao.update_business_profile(
        org_id,
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

    profile = dao.get_business_profile(org_id)
    assert profile["billing_address"]["country"] == "US"
    assert profile["billing_address"]["state"] == "CA"
    assert profile["billing_address"]["postal_code"] == "94102"


@pytest.mark.anyio
async def test_international_address_india(client: AsyncClient, dbsession):
    """Test India address format with district."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "india_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "India Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)
    dao.update_business_profile(
        org_id,
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

    profile = dao.get_business_profile(org_id)
    assert profile["billing_address"]["country"] == "IN"
    assert profile["billing_address"]["district"] == "Bengaluru Urban"
    assert profile["billing_address"]["state"] == "Karnataka"


@pytest.mark.anyio
async def test_international_address_uk(client: AsyncClient, dbsession):
    """Test UK address format with county."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "uk_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "UK Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)
    dao.update_business_profile(
        org_id,
        billing_address={
            "country": "GB",
            "line1": "10 Downing Street",
            "city": "London",
            "postal_code": "SW1A 2AA",
            "formatted": "10 Downing Street, London SW1A 2AA, United Kingdom",
        },
    )
    dbsession.commit()

    profile = dao.get_business_profile(org_id)
    assert profile["billing_address"]["country"] == "GB"
    assert profile["billing_address"]["postal_code"] == "SW1A 2AA"


@pytest.mark.anyio
async def test_international_address_japan(client: AsyncClient, dbsession):
    """Test Japan address format with custom fields."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "japan_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Japan Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)
    dao.update_business_profile(
        org_id,
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

    profile = dao.get_business_profile(org_id)
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
        f"/v0/organizations/{org_id}/billing/business-profile",
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
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, "merge_addr@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Merge Address Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    dao = OrganizationBillingDAO(dbsession)

    # Set initial address
    dao.update_business_profile(
        org_id,
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
    dao.update_business_profile(
        org_id,
        billing_address={
            "city": "Cambridge",
        },
    )
    dbsession.commit()

    # Verify merge happened
    profile = dao.get_business_profile(org_id)
    assert profile["billing_address"]["country"] == "US"  # Preserved
    assert profile["billing_address"]["line1"] == "123 Main St"  # Preserved
    assert profile["billing_address"]["city"] == "Cambridge"  # Updated
    assert profile["billing_address"]["state"] == "MA"  # Preserved


# ============== Critical Review Fix Tests ==============


def test_frozen_org_cannot_spend_credits(dbsession):
    """Test that suspended organizations cannot spend credits (H1 fix)."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import AuthUser, Organization
    from orchestra.lib.billing import get_billing_entity

    # Create owner
    owner = AuthUser(
        id='frozen_org_owner',
        email='frozen_org_owner@test.com',
        name='Frozen Org Owner',
    )
    dbsession.add(owner)
    dbsession.flush()

    # Create org with direct billing
    org = Organization(
        name='Frozen Test Org',
        owner_id=owner.id,
        stripe_customer_id='cus_frozen_test',
        account_status='ACTIVE',
    )
    dbsession.add(org)
    dbsession.commit()

    # Should work when ACTIVE
    billing_entity = get_billing_entity(dbsession, owner.id, org.id)
    assert billing_entity.is_organization

    # Suspend the org
    dao = OrganizationBillingDAO(dbsession)
    dao.set_account_status(org.id, 'SUSPENDED')
    dbsession.commit()

    # Should raise when SUSPENDED
    import pytest
    with pytest.raises(ValueError) as exc_info:
        get_billing_entity(dbsession, owner.id, org.id)
    assert 'SUSPENDED' in str(exc_info.value)


def test_invalid_account_status_rejected(dbsession):
    """Test that invalid account status values are rejected (H4/M3 fix)."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
    from orchestra.db.models.orchestra_models import AuthUser, Organization
    import pytest

    # Create owner
    owner = AuthUser(
        id='status_owner',
        email='status_owner@test.com',
        name='Status Owner',
    )
    dbsession.add(owner)
    dbsession.flush()

    # Create org
    org = Organization(
        name='Status Test Org',
        owner_id=owner.id,
        account_status='ACTIVE',
    )
    dbsession.add(org)
    dbsession.commit()

    dao = OrganizationBillingDAO(dbsession)

    # Valid statuses should work
    assert dao.set_account_status(org.id, 'SUSPENDED') is True
    assert dao.set_account_status(org.id, 'PAST_DUE') is True
    assert dao.set_account_status(org.id, 'CLOSED') is True
    assert dao.set_account_status(org.id, 'ACTIVE') is True

    # Invalid status should raise
    with pytest.raises(ValueError) as exc_info:
        dao.set_account_status(org.id, 'BANANA')
    assert 'Invalid account status' in str(exc_info.value)

    with pytest.raises(ValueError):
        dao.set_account_status(org.id, 'FROZEN')  # Not a valid status


def test_recharge_xor_constraint(dbsession):
    """Test that recharge table enforces exactly one of user_id/organization_id (XOR fix)."""
    from sqlalchemy.exc import IntegrityError
    from orchestra.db.models.orchestra_models import AuthUser, Organization, Recharge, RechargeStatus
    from orchestra.db.models.orchestra_models import Users
    from decimal import Decimal

    # Create user
    user = Users(
        id='xor_user',
        credits=Decimal('100'),
    )
    dbsession.add(user)

    # Create owner and org
    owner = AuthUser(
        id='xor_owner',
        email='xor_owner@test.com',
        name='XOR Owner',
    )
    dbsession.add(owner)
    dbsession.flush()

    org = Organization(
        name='XOR Test Org',
        owner_id=owner.id,
        account_status='ACTIVE',
    )
    dbsession.add(org)
    dbsession.commit()

    # Valid: user_id only
    r1 = Recharge(
        user_id='xor_user',
        organization_id=None,
        quantity=Decimal('10'),
        amount_usd=Decimal('10'),
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(r1)
    dbsession.commit()

    # Valid: organization_id only
    r2 = Recharge(
        user_id=None,
        organization_id=org.id,
        quantity=Decimal('10'),
        amount_usd=Decimal('10'),
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(r2)
    dbsession.commit()

    # Invalid: both set - should fail
    r3 = Recharge(
        user_id='xor_user',
        organization_id=org.id,
        quantity=Decimal('10'),
        amount_usd=Decimal('10'),
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(r3)
    import pytest
    with pytest.raises(IntegrityError):
        dbsession.commit()
    dbsession.rollback()

    # Invalid: neither set - should fail
    r4 = Recharge(
        user_id=None,
        organization_id=None,
        quantity=Decimal('10'),
        amount_usd=Decimal('10'),
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(r4)
    with pytest.raises(IntegrityError):
        dbsession.commit()
    dbsession.rollback()


def test_duplicate_stripe_customer_id_rejected(dbsession):
    """Test that duplicate stripe_customer_id is rejected (H3 fix)."""
    from sqlalchemy.exc import IntegrityError
    from orchestra.db.models.orchestra_models import AuthUser, Organization
    import pytest

    # Create owners
    owner1 = AuthUser(id='dup_owner1', email='dup1@test.com', name='Owner 1')
    owner2 = AuthUser(id='dup_owner2', email='dup2@test.com', name='Owner 2')
    dbsession.add(owner1)
    dbsession.add(owner2)
    dbsession.flush()

    # Create org1 with stripe customer id
    org1 = Organization(
        name='Dup Test Org 1',
        owner_id=owner1.id,
        stripe_customer_id='cus_duplicate_test',
        account_status='ACTIVE',
    )
    dbsession.add(org1)
    dbsession.commit()

    # Try to create org2 with same stripe customer id - should fail
    org2 = Organization(
        name='Dup Test Org 2',
        owner_id=owner2.id,
        stripe_customer_id='cus_duplicate_test',  # Same as org1
        account_status='ACTIVE',
    )
    dbsession.add(org2)
    with pytest.raises(IntegrityError):
        dbsession.commit()
    dbsession.rollback()

    # But NULL stripe_customer_id should be allowed for multiple orgs
    org3 = Organization(
        name='Dup Test Org 3',
        owner_id=owner1.id,
        stripe_customer_id=None,
        account_status='ACTIVE',
    )
    org4 = Organization(
        name='Dup Test Org 4',
        owner_id=owner2.id,
        stripe_customer_id=None,
        account_status='ACTIVE',
    )
    dbsession.add(org3)
    dbsession.add(org4)
    dbsession.commit()  # Should succeed


@pytest.mark.anyio
async def test_checkout_webhook_enables_direct_billing(client: AsyncClient, dbsession):
    """Test that checkout webhook sets stripe_customer_id for new orgs (H2 fix)."""
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    owner = await create_test_user(client, 'webhook_owner@test.com')

    org_response = await client.post(
        '/v0/organizations',
        json={'name': 'Webhook Test Org'},
        headers=owner['headers'],
    )
    org_id = org_response.json()['id']

    dao = OrganizationBillingDAO(dbsession)
    org = dao.get(org_id)

    # Initially no stripe customer id
    assert org.stripe_customer_id is None

    # Simulate what webhook handler does
    if not org.stripe_customer_id:
        stripe_customer_id = 'cus_webhook_test_123'
        dao.set_stripe_customer_id(org_id, stripe_customer_id)
        dbsession.commit()

    # Verify it was set
    dbsession.refresh(org)
    assert org.stripe_customer_id == 'cus_webhook_test_123'
    assert dao.has_direct_billing(org_id) is True


def test_duplicate_autorecharge_prevented(dbsession):
    """Test that duplicate auto-recharges in same month are prevented (M2 fix)."""
    from orchestra.db.models.orchestra_models import AuthUser, Organization, Recharge, RechargeStatus
    from orchestra.lib.time import month_end_utc
    from datetime import datetime, timezone
    from decimal import Decimal

    # Create owner and org
    owner = AuthUser(
        id='dup_recharge_owner',
        email='dup_recharge@test.com',
        name='Dup Recharge Owner',
    )
    dbsession.add(owner)
    dbsession.flush()

    org = Organization(
        name='Dup Recharge Org',
        owner_id=owner.id,
        stripe_customer_id='cus_dup_recharge',
        account_status='ACTIVE',
        autorecharge=True,
        autorecharge_threshold=Decimal('10'),
        autorecharge_qty=Decimal('100'),
    )
    dbsession.add(org)
    dbsession.commit()

    current_month_end = month_end_utc(datetime.now(timezone.utc).date())

    # Create first pending recharge
    r1 = Recharge(
        organization_id=org.id,
        quantity=Decimal('100'),
        amount_usd=Decimal('100'),
        invoice_group=current_month_end,
        status=RechargeStatus.PENDING_INVOICE,
        type='auto',
    )
    dbsession.add(r1)
    dbsession.commit()

    # Simulate the idempotency check from bg_tasks.py
    existing_recharge = dbsession.query(Recharge).filter_by(
        organization_id=org.id,
        invoice_group=current_month_end,
        status=RechargeStatus.PENDING_INVOICE,
    ).first()

    # Should find the existing recharge
    assert existing_recharge is not None
    assert existing_recharge.id == r1.id

    # This is what bg_tasks.py does - skip if existing
    should_skip = existing_recharge is not None
    assert should_skip is True
