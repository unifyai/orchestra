"""Tests for Phase 3: RBAC Application - Teams and Resource Access."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.tests.utils import create_test_user


@pytest.mark.anyio
async def test_create_team(client: AsyncClient):
    """Test creating a team in an organization."""
    owner = await create_test_user(client, "team_create_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team Test Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Create team
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Engineering", "description": "Engineering team"},
        headers=owner["headers"],
    )
    assert team_response.status_code == status.HTTP_201_CREATED

    team = team_response.json()
    assert team["name"] == "Engineering"
    assert team["description"] == "Engineering team"
    assert team["organization_id"] == org_id
    assert team["member_count"] == 0


@pytest.mark.anyio
async def test_cannot_create_duplicate_team(client: AsyncClient):
    """Test that duplicate team names are not allowed."""
    owner = await create_test_user(client, "team_dup_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Duplicate Team Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create first team
    await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Sales"},
        headers=owner["headers"],
    )

    # Try to create duplicate
    response2 = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Sales"},
        headers=owner["headers"],
    )
    assert response2.status_code == status.HTTP_409_CONFLICT


@pytest.mark.anyio
async def test_non_owner_cannot_create_team(client: AsyncClient):
    """Test that non-owners cannot create teams."""
    owner = await create_test_user(client, "team_perm_owner@test.com")
    member = await create_test_user(client, "team_perm_member@test.com")

    # Create organization and add member
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team Permission Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Try to create team as member
    response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Unauthorized Team"},
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_list_teams(client: AsyncClient):
    """Test listing teams in an organization."""
    owner = await create_test_user(client, "team_list_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team List Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create multiple teams
    await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Team A"},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Team B"},
        headers=owner["headers"],
    )

    # List teams
    response = await client.get(
        f"/v0/organizations/{org_id}/teams",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    teams = response.json()
    assert len(teams) == 2
    team_names = [t["name"] for t in teams]
    assert "Team A" in team_names
    assert "Team B" in team_names


@pytest.mark.anyio
async def test_add_team_members(client: AsyncClient):
    """Test adding members to a team."""
    owner = await create_test_user(client, "team_member_owner@test.com")
    user1 = await create_test_user(client, "team_member1@test.com")
    user2 = await create_test_user(client, "team_member2@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team Member Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add users to organization
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user1["id"], "level": "user"},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user2["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create team
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Test Team"},
        headers=owner["headers"],
    )
    team_id = team_response.json()["id"]

    # Add members to team one by one
    response1 = await client.post(
        f"/v0/organizations/{org_id}/teams/{team_id}/members",
        json={"user_ids": [user1["id"]]},
        headers=owner["headers"],
    )
    if response1.status_code != 200:
        print(f"ERROR adding user1: {response1.status_code} - {response1.json()}")
    assert response1.status_code == 200
    assert user1["id"] in response1.json()["members"]

    response2 = await client.post(
        f"/v0/organizations/{org_id}/teams/{team_id}/members",
        json={"user_ids": [user2["id"]]},
        headers=owner["headers"],
    )
    if response2.status_code != 200:
        print(f"ERROR adding user2: {response2.status_code} - {response2.json()}")
    assert response2.status_code == 200
    assert user2["id"] in response2.json()["members"]

    # Verify both members are in the team
    team_response = await client.get(
        f"/v0/organizations/{org_id}/teams/{team_id}",
        headers=owner["headers"],
    )
    team = team_response.json()
    assert len(team["members"]) == 2
    assert user1["id"] in team["members"]
    assert user2["id"] in team["members"]


@pytest.mark.anyio
async def test_cannot_add_non_org_member_to_team(client: AsyncClient):
    """Test that only org members can be added to teams."""
    owner = await create_test_user(client, "team_non_member_owner@test.com")
    outsider = await create_test_user(client, "outsider@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team Non-Member Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create team
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Exclusive Team"},
        headers=owner["headers"],
    )
    team_id = team_response.json()["id"]

    # Try to add non-org-member to team
    response = await client.post(
        f"/v0/organizations/{org_id}/teams/{team_id}/members",
        json={"user_ids": [outsider["id"]]},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_remove_team_member(client: AsyncClient):
    """Test removing a member from a team."""
    owner = await create_test_user(client, "team_remove_owner@test.com")
    user = await create_test_user(client, "team_remove_user@test.com")

    # Create organization and team
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team Remove Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user["id"], "level": "user"},
        headers=owner["headers"],
    )

    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Test Team"},
        headers=owner["headers"],
    )
    team_id = team_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/teams/{team_id}/members",
        json={"user_ids": [user["id"]]},
        headers=owner["headers"],
    )

    # Remove member
    response = await client.delete(
        f"/v0/organizations/{org_id}/teams/{team_id}/members/{user['id']}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK

    team = response.json()
    assert user["id"] not in team["members"]


@pytest.mark.anyio
async def test_delete_team(client: AsyncClient):
    """Test deleting a team."""
    owner = await create_test_user(client, "team_delete_owner@test.com")

    # Create organization and team
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Team Delete Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "To Delete"},
        headers=owner["headers"],
    )
    team_id = team_response.json()["id"]

    # Delete team
    response = await client.delete(
        f"/v0/organizations/{org_id}/teams/{team_id}",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT

    # Verify team is deleted
    get_response = await client.get(
        f"/v0/organizations/{org_id}/teams/{team_id}",
        headers=owner["headers"],
    )
    assert get_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_personal_project_creator_has_full_access(client: AsyncClient, dbsession):
    """Test that personal project creator has implicit full access."""
    user = await create_test_user(client, "personal_owner@test.com")

    # Create personal project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=user["id"],
        name="My_Personal_Project",
        organization_id=None,  # Personal project
    )
    dbsession.commit()
    projects = project_dao.filter(user_id=user["id"], name="My_Personal_Project")
    project = projects[0][0]

    # Check permissions via DAO
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Should have read permission
    has_read = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert has_read is True

    # Should have write permission
    has_write = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:write",
    )
    assert has_write is True

    # Should have delete permission
    has_delete = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:delete",
    )
    assert has_delete is True


@pytest.mark.anyio
async def test_personal_project_other_users_no_access(client: AsyncClient, dbsession):
    """Test that other users have no access to personal projects."""
    owner = await create_test_user(client, "personal_project_owner@test.com")
    other = await create_test_user(client, "other_user@test.com")

    # Create personal project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=owner["id"],
        name="Private_Project",
        organization_id=None,  # Personal project
    )
    dbsession.commit()
    projects = project_dao.filter(user_id=owner["id"], name="Private_Project")
    project = projects[0][0]

    # Check other user has no permission
    resource_access_dao = ResourceAccessDAO(dbsession)

    has_read = resource_access_dao.check_user_permission(
        other["id"],
        "project",
        project.id,
        "project:read",
    )
    assert has_read is False


@pytest.mark.anyio
async def test_cannot_share_personal_project(client: AsyncClient, dbsession):
    """Test that personal projects cannot be shared via RBAC."""
    owner = await create_test_user(client, "personal_no_share_owner@test.com")
    other = await create_test_user(client, "personal_no_share_other@test.com")

    # Create personal project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        user_id=owner["id"],
        name="Cannot_Share",
        organization_id=None,  # Personal project
    )
    dbsession.commit()
    projects = project_dao.filter(user_id=owner["id"], name="Cannot_Share")
    project = projects[0][0]

    # Get a role ID
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Try to grant access to personal project
    response = await client.post(
        f"/v0/resources/project/{project.id}/access",
        json={
            "role_id": viewer_role.id,
            "grantee_type": "user",
            "grantee_id": other["id"],
        },
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "personal" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_org_project_creator_gets_owner_role(client: AsyncClient, dbsession):
    """Test that org project creator gets Owner role via ResourceAccess."""
    owner = await create_test_user(client, "org_project_creator@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Project Creator Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create org project using DAO
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    # Create org project
    project_dao.create(
        name="Org_Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Org_Project")
    project = projects[0][0]

    # Grant Owner role to creator
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=owner_role.id,
        grantee_type="user",
        grantee_id=owner["id"],
    )
    dbsession.commit()

    # Check creator has full access
    has_read = resource_access_dao.check_user_permission(
        owner["id"],
        "project",
        project.id,
        "project:read",
    )
    assert has_read is True

    has_delete = resource_access_dao.check_user_permission(
        owner["id"],
        "project",
        project.id,
        "project:delete",
    )
    assert has_delete is True


@pytest.mark.anyio
async def test_share_org_project_with_user(client: AsyncClient, dbsession):
    """Test sharing an org project with a user."""
    owner = await create_test_user(client, "share_project_owner@test.com")
    collaborator = await create_test_user(client, "collaborator@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Share Project Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add collaborator to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": collaborator["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create org project and grant access manually
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Shared_Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Shared_Project")
    project = projects[0][0]

    # Grant Owner to creator
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=owner_role.id,
        grantee_type="user",
        grantee_id=owner["id"],
    )

    # Grant Member role to collaborator
    member_role = role_dao.get_by_name("Member", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=member_role.id,
        grantee_type="user",
        grantee_id=collaborator["id"],
    )
    dbsession.commit()

    # Check collaborator has read/write but not delete
    has_read = resource_access_dao.check_user_permission(
        collaborator["id"],
        "project",
        project.id,
        "project:read",
    )
    assert has_read is True

    has_write = resource_access_dao.check_user_permission(
        collaborator["id"],
        "project",
        project.id,
        "project:write",
    )
    assert has_write is True

    has_delete = resource_access_dao.check_user_permission(
        collaborator["id"],
        "project",
        project.id,
        "project:delete",
    )
    assert has_delete is False  # Members can't delete


@pytest.mark.anyio
async def test_share_org_project_with_team(client: AsyncClient, dbsession):
    """Test sharing an org project with a team."""
    owner = await create_test_user(client, "share_team_owner@test.com")
    team_member = await create_test_user(client, "team_member_share@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Share Team Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add team member to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": team_member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create team
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Dev Team"},
        headers=owner["headers"],
    )
    team_id = team_response.json()["id"]

    # Add member to team
    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/teams/{team_id}/members",
        json={"user_ids": [team_member["id"]]},
        headers=owner["headers"],
    )
    assert add_member_response.status_code == 200, "Failed to add member to team"

    # Create org project and grant team access
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Team_Shared_Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Team_Shared_Project")
    project = projects[0][0]

    # Grant Owner to creator
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=owner_role.id,
        grantee_type="user",
        grantee_id=owner["id"],
    )

    # Grant Viewer role to team
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="team",
        grantee_id=str(team_id),
    )
    dbsession.commit()

    # Check team member has read access (via team)
    has_read = resource_access_dao.check_user_permission(
        team_member["id"],
        "project",
        project.id,
        "project:read",
    )
    assert has_read is True

    # But not write access (Viewer role)
    has_write = resource_access_dao.check_user_permission(
        team_member["id"],
        "project",
        project.id,
        "project:write",
    )
    assert has_write is False


@pytest.mark.anyio
async def test_filter_accessible_resources(client: AsyncClient, dbsession):
    """Test that filter_accessible_resources returns both personal and org projects."""
    user = await create_test_user(client, "filter_access_user@test.com")

    # Initialize DAOs
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    # Create personal project
    project_dao.create(
        user_id=user["id"],
        name="Personal_Project",
        organization_id=None,
    )
    dbsession.commit()
    personal_projects = project_dao.filter(user_id=user["id"], name="Personal_Project")
    personal_project = personal_projects[0][0]

    # Create organization and org project
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Filter Access Org"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    project_dao.create(
        name="Org_Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()
    org_projects = project_dao.filter(organization_id=org_id, name="Org_Project")
    org_project = org_projects[0][0]

    # Grant access to org project
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=org_project.id,
        role_id=owner_role.id,
        grantee_type="user",
        grantee_id=user["id"],
    )
    dbsession.commit()

    # Filter accessible projects
    accessible_ids = resource_access_dao.filter_accessible_resources(
        user["id"],
        "project",
        "project:read",
    )

    # Should include both personal and org projects
    assert personal_project.id in accessible_ids
    assert org_project.id in accessible_ids


@pytest.mark.anyio
async def test_revoke_resource_access(client: AsyncClient, dbsession):
    """Test revoking access to a resource."""
    owner = await create_test_user(client, "revoke_access_owner@test.com")
    user = await create_test_user(client, "revoke_access_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Revoke Access Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add user to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create org project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Revoke_Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Revoke_Project")
    project = projects[0][0]

    # Grant Owner to creator
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=owner_role.id,
        grantee_type="user",
        grantee_id=owner["id"],
    )

    # Grant Member to user
    member_role = role_dao.get_by_name("Member", organization_id=None)
    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=member_role.id,
        grantee_type="user",
        grantee_id=user["id"],
    )
    dbsession.commit()

    # Verify user has access
    has_access_before = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert has_access_before is True

    # Revoke access
    resource_access_dao.revoke_access(
        resource_type="project",
        resource_id=project.id,
        grantee_type="user",
        grantee_id=user["id"],
    )
    dbsession.commit()

    # Verify user no longer has access
    has_access_after = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project.id,
        "project:read",
    )
    assert has_access_after is False


@pytest.mark.anyio
async def test_org_member_implicit_access_no_teams(client: AsyncClient, dbsession):
    """
    Test that organization members have implicit access to org resources
    even without explicit ResourceAccess grants or team memberships.

    This ensures organizations work seamlessly without teams or manual sharing.
    """
    owner = await create_test_user(client, "org_implicit_owner@test.com")
    member = await create_test_user(client, "org_implicit_member@test.com")
    non_member = await create_test_user(client, "non_member@test.com")

    # Initialize DAOs
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    org_dao = OrganizationDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Implicit Access Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member to organization (no teams, no explicit resource sharing)
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create an organizational project (no ResourceAccess entries)
    project_dao.create(
        name="Org_Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Org_Project")
    project = projects[0][0]
    project_id = project.id

    # OWNER should have all permissions (implicit Owner role)
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

    # MEMBER should have read/write permissions (implicit Member role)
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    ), "Organization member should have read access without explicit grant"

    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    ), "Organization member should have write access without explicit grant"

    # MEMBER should NOT have delete permission (Member role doesn't include delete)
    assert not resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:delete",
    ), "Organization member should NOT have delete permission"

    # NON-MEMBER should have NO access
    assert not resource_access_dao.check_user_permission(
        non_member["id"],
        "project",
        project_id,
        "project:read",
    ), "Non-member should have no access"


@pytest.mark.anyio
async def test_explicit_grant_overrides_implicit_access(client: AsyncClient, dbsession):
    """
    Test that explicit ResourceAccess grants REPLACE implicit organization membership.

    When a resource has explicit grants, only those grants apply (no implicit fallback).
    """
    owner = await create_test_user(client, "org_explicit_owner@test.com")
    member = await create_test_user(client, "org_explicit_member@test.com")

    # Initialize DAOs
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    org_dao = OrganizationDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Create organization and add member
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Explicit Grant Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create org project
    project_dao.create(
        name="Restricted_Project",
        user_id=None,  # Org projects don't have user_id
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Restricted_Project")
    project = projects[0][0]
    project_id = project.id

    # WITHOUT explicit grant: member has read/write (implicit Member role)
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    ), "Should have read from implicit Member role"
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    ), "Should have write from implicit Member role"

    # NOW grant explicit Viewer role (read-only)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        "project",
        project_id,
        viewer_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # WITH explicit grant: member has ONLY what the explicit grant provides
    # (Explicit grants REPLACE implicit membership, not add to it)
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:read",
    ), "Should have read from explicit Viewer grant"

    assert not resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project_id,
        "project:write",
    ), "Should NOT have write - explicit Viewer grant replaces implicit Member role"


@pytest.mark.anyio
async def test_only_project_org_resource_types_allowed(client: AsyncClient, dbsession):
    """Test that only 'project' and 'org' resource types are allowed."""
    owner = await create_test_user(client, "invalid_resource_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Invalid Resource Test Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Create a team
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Test Team"},
        headers=owner["headers"],
    )
    assert team_response.status_code == status.HTTP_201_CREATED
    team_id = team_response.json()["id"]

    # Get system role
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Try to grant access to invalid resource types
    invalid_types = ["interface", "tab", "tile", "invalid"]

    for invalid_type in invalid_types:
        response = await client.post(
            f"/v0/resources/{invalid_type}/999/access",
            json={
                "role_id": viewer_role.id,
                "grantee_type": "team",
                "grantee_id": str(team_id),
            },
            headers=owner["headers"],
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid resource type" in response.json()["detail"]
        assert "Only 'project' and 'org' are supported" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_resource_access_role(client: AsyncClient, dbsession):
    """Test updating a resource access grant's role (happy path)."""
    owner = await create_test_user(client, "update_access_owner@test.com")
    member = await create_test_user(client, "update_access_member@test.com")

    # Create org
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Update Access Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Test_Update_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Test_Update_Project")
    project = projects[0][0]

    # Grant Viewer role to member
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    access = resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()
    access_id = access.id
    original_created_at = access.created_at

    # Verify member has only read access
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:read",
    ), "Member should have read permission with Viewer role"
    assert not resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:write",
    ), "Member should not have write permission with Viewer role"

    # Update to Member role (has write permission)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    update_response = await client.patch(
        f"/v0/resources/project/{project.id}/access/{access_id}",
        json={"role_id": member_role.id},
        headers=owner["headers"],
    )
    assert (
        update_response.status_code == 200
    ), f"Update failed: {update_response.json()}"

    updated_data = update_response.json()
    assert updated_data["id"] == access_id, "Access ID should be preserved"
    assert updated_data["role_id"] == member_role.id, "Role ID should be updated"
    assert updated_data["role_name"] == "Member", "Role name should be 'Member'"
    assert updated_data["grantee_id"] == member["id"], "Grantee should remain the same"
    assert (
        updated_data["created_at"] == original_created_at.isoformat()
    ), "created_at should be preserved"

    # Verify member now has write access
    dbsession.expire_all()  # Clear SQLAlchemy cache
    resource_access_dao.clear_permission_cache()  # Clear permission cache

    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:read",
    ), "Member should still have read permission"
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:write",
    ), "Member should now have write permission"


@pytest.mark.anyio
async def test_update_resource_access_team(client: AsyncClient, dbsession):
    """Test updating a team's resource access role."""
    owner = await create_test_user(client, "update_team_access_owner@test.com")
    member = await create_test_user(client, "update_team_access_member@test.com")

    # Create org
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Update Team Access Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create team and add member
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Engineering Team"},
        headers=owner["headers"],
    )
    team_id = team_response.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/teams/{team_id}/members",
        json={"user_ids": [member["id"]]},
        headers=owner["headers"],
    )

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Team_Access_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Team_Access_Project")
    project = projects[0][0]

    # Grant Viewer role to team
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    access = resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="team",
        grantee_id=str(team_id),
    )
    dbsession.commit()
    access_id = access.id

    # Verify team member has only read access
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:read",
    )
    assert not resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:write",
    )

    # Update team to Admin role
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    update_response = await client.patch(
        f"/v0/resources/project/{project.id}/access/{access_id}",
        json={"role_id": admin_role.id},
        headers=owner["headers"],
    )
    assert update_response.status_code == 200

    # Verify team member now has write and delete access
    dbsession.expire_all()
    resource_access_dao.clear_permission_cache()

    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:write",
    ), "Team member should have write permission via team's Admin role"
    assert resource_access_dao.check_user_permission(
        member["id"],
        "project",
        project.id,
        "project:delete",
    ), "Team member should have delete permission via team's Admin role"


@pytest.mark.anyio
async def test_update_resource_access_invalid_id(client: AsyncClient, dbsession):
    """Test updating a non-existent access grant returns 404."""
    owner = await create_test_user(client, "invalid_update_owner@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Invalid Update Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Try to update non-existent access grant
    response = await client.patch(
        f"/v0/resources/org/{org_id}/access/99999",
        json={"role_id": viewer_role.id},
        headers=owner["headers"],
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_resource_access_wrong_resource(client: AsyncClient, dbsession):
    """Test updating an access grant with mismatched resource returns 404."""
    owner = await create_test_user(client, "wrong_resource_owner@test.com")

    # Create two orgs
    org1_response = await client.post(
        "/v0/organizations",
        json={"name": "Wrong Resource Org 1"},
        headers=owner["headers"],
    )
    org1_id = org1_response.json()["id"]

    org2_response = await client.post(
        "/v0/organizations",
        json={"name": "Wrong Resource Org 2"},
        headers=owner["headers"],
    )
    org2_id = org2_response.json()["id"]

    # Grant access to org1
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    access = resource_access_dao.grant_access(
        resource_type="org",
        resource_id=org1_id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=owner["id"],
    )
    dbsession.commit()

    # Try to update via org2's access endpoint (wrong resource)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    response = await client.patch(
        f"/v0/resources/org/{org2_id}/access/{access.id}",
        json={"role_id": member_role.id},
        headers=owner["headers"],
    )
    assert response.status_code == 404
    assert "does not belong to this resource" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_resource_access_requires_permission(
    client: AsyncClient,
    dbsession,
):
    """Test that updating access requires write permission."""
    owner = await create_test_user(client, "update_perm_owner@test.com")
    viewer_user = await create_test_user(client, "update_perm_viewer@test.com")

    # Create org
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Update Permission Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add viewer_user with Viewer role (no write permission)
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer_user["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Update viewer's role to actual Viewer role
    await client.patch(
        f"/v0/organizations/{org_id}/members/{viewer_user['id']}/role",
        json={"role_id": viewer_role.id},
        headers=owner["headers"],
    )

    # Create project and grant owner some access
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

    access = resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=viewer_user["id"],
    )
    dbsession.commit()

    # Viewer tries to update access → should fail (no write permission)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    response = await client.patch(
        f"/v0/resources/project/{project.id}/access/{access.id}",
        json={"role_id": member_role.id},
        headers=viewer_user["headers"],
    )
    assert response.status_code == 403
    assert "permission" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_resource_access_duplicate_constraint(
    client: AsyncClient,
    dbsession,
):
    """Test that updating to a duplicate grant returns 409 Conflict."""
    owner = await create_test_user(client, "duplicate_update_owner@test.com")
    member = await create_test_user(client, "duplicate_update_member@test.com")

    # Create org
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Duplicate Update Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Duplicate_Test_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Duplicate_Test_Project")
    project = projects[0][0]

    # Grant member BOTH Viewer and Member roles (two separate grants)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    viewer_access = resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )

    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=member_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Try to update Viewer grant to Member role (which already exists) → should fail
    response = await client.patch(
        f"/v0/resources/project/{project.id}/access/{viewer_access.id}",
        json={"role_id": member_role.id},
        headers=owner["headers"],
    )
    assert response.status_code == 409
    assert "already has" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_resource_access_invalid_role(client: AsyncClient, dbsession):
    """Test that updating with non-existent role returns 404."""
    owner = await create_test_user(client, "invalid_role_owner@test.com")

    # Create org
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Invalid Role Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create project and grant
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Invalid_Role_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Invalid_Role_Project")
    project = projects[0][0]

    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    access = resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=owner["id"],
    )
    dbsession.commit()

    # Try to update with non-existent role ID
    response = await client.patch(
        f"/v0/resources/project/{project.id}/access/{access.id}",
        json={"role_id": 99999},
        headers=owner["headers"],
    )
    assert response.status_code == 404
    assert "role" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_resource_access_invalid_resource_type(
    client: AsyncClient,
    dbsession,
):
    """Test that updating with invalid resource type returns 400."""
    owner = await create_test_user(client, "invalid_type_owner@test.com")

    # Try to update access for invalid resource type
    response = await client.patch(
        "/v0/resources/interface/123/access/1",
        json={"role_id": 1},
        headers=owner["headers"],
    )
    assert response.status_code == 400
    assert "Invalid resource type" in response.json()["detail"]
    assert "Only 'project' and 'org' are supported" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_resource_access_preserves_grantee(client: AsyncClient, dbsession):
    """Test that updating only changes role, not grantee."""
    owner = await create_test_user(client, "preserve_grantee_owner@test.com")
    member1 = await create_test_user(client, "preserve_member1@test.com")
    member2 = await create_test_user(client, "preserve_member2@test.com")

    # Create org and add members
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Preserve Grantee Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member1["id"], "level": "user"},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member2["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Preserve_Grantee_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(
        organization_id=org_id,
        name="Preserve_Grantee_Project",
    )
    project = projects[0][0]

    # Grant member1 Viewer role
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    access = resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=member1["id"],
    )
    dbsession.commit()

    # Update role
    member_role = role_dao.get_by_name("Member", organization_id=None)
    update_response = await client.patch(
        f"/v0/resources/project/{project.id}/access/{access.id}",
        json={"role_id": member_role.id},
        headers=owner["headers"],
    )
    assert update_response.status_code == 200

    # Verify grantee is still member1, not changed to member2
    updated_data = update_response.json()
    assert (
        updated_data["grantee_id"] == member1["id"]
    ), "Grantee should not change during update"
    assert updated_data["grantee_type"] == "user", "Grantee type should not change"

    # Verify member1 has access, member2 does not
    dbsession.expire_all()
    resource_access_dao.clear_permission_cache()

    assert resource_access_dao.check_user_permission(
        member1["id"],
        "project",
        project.id,
        "project:write",
    ), "member1 should have write access"
    assert not resource_access_dao.check_user_permission(
        member2["id"],
        "project",
        project.id,
        "project:write",
    ), "member2 should not have access"
