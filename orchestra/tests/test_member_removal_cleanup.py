"""Tests for member removal cleanup behavior.

When a member is removed from an organization, the following should happen:
1. ResourceAccess entries for the user are revoked
2. TeamMember entries are removed
3. Unshared resources created by the user are deleted
4. Shared resources created by the user are kept
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.models.orchestra_models import ResourceAccess, TeamMember
from orchestra.tests.utils import create_test_user


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Automatically mock assistant infrastructure webhooks for all tests."""
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
    ) as mock_reawaken, patch(
        "orchestra.web.api.assistant.views.stop_jobs",
    ) as mock_stop_jobs, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_stop_jobs.return_value = MagicMock(status_code=200)
        mock_settings.is_staging = True

        yield mock_wake_up, mock_reawaken, mock_stop_jobs


# =============================================================================
# ResourceAccess Cleanup Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_revokes_resource_access(client: AsyncClient, dbsession):
    """Test that removing a member revokes their ResourceAccess entries."""
    owner = await create_test_user(client, "cleanup_owner@test.com")
    member = await create_test_user(client, "cleanup_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Cleanup Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create org project and grant explicit access to member
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Shared Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Shared Project")
    project = projects[0][0]

    # Grant explicit Viewer access to member
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        "project",
        project.id,
        viewer_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # Verify member has access entry
    access_before = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.grantee_type == "user",
            ResourceAccess.grantee_id == member["id"],
            ResourceAccess.resource_type == "project",
            ResourceAccess.resource_id == project.id,
        )
        .first()
    )
    assert access_before is not None, "Member should have ResourceAccess entry"

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify ResourceAccess entry is removed
    dbsession.expire_all()
    access_after = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.grantee_type == "user",
            ResourceAccess.grantee_id == member["id"],
            ResourceAccess.resource_type == "project",
            ResourceAccess.resource_id == project.id,
        )
        .first()
    )
    assert access_after is None, "ResourceAccess entry should be removed"


# =============================================================================
# TeamMember Cleanup Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_removes_from_teams(client: AsyncClient, dbsession):
    """Test that removing a member removes them from all org teams."""
    owner = await create_test_user(client, "team_cleanup_owner@test.com")
    member = await create_test_user(client, "team_cleanup_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Team Cleanup Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create teams and add member
    team_dao = TeamDAO(dbsession)
    team1 = team_dao.create(name="Engineering", organization_id=org_id)
    team2 = team_dao.create(name="Design", organization_id=org_id)
    team_dao.add_member(team1.id, member["id"])
    team_dao.add_member(team2.id, member["id"])
    dbsession.commit()

    # Verify member is in teams
    teams_before = (
        dbsession.query(TeamMember).filter(TeamMember.user_id == member["id"]).all()
    )
    assert len(teams_before) == 2, "Member should be in 2 teams"

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify member is removed from all teams
    dbsession.expire_all()
    teams_after = (
        dbsession.query(TeamMember).filter(TeamMember.user_id == member["id"]).all()
    )
    assert len(teams_after) == 0, "Member should be removed from all teams"


# =============================================================================
# Unshared Resource Deletion Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_deletes_unshared_project(client: AsyncClient, dbsession):
    """Test that removing a member deletes projects they created but never shared."""
    owner = await create_test_user(client, "unshared_proj_owner@test.com")
    member = await create_test_user(
        client,
        "unshared_proj_member@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Unshared Project Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Member creates a project with explicit access only for themselves
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    # Create project with member as creator
    project_dao.create(
        name="Private Project",
        user_id=member["id"],  # Member is the creator
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Private Project")
    project = projects[0][0]
    project_id = project.id

    # Grant explicit access ONLY to the creator (making it private/unshared)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        "project",
        project_id,
        owner_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # Verify project exists
    project_before = project_dao.get(project_id)
    assert project_before is not None, "Project should exist"

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify project is deleted
    dbsession.expire_all()
    project_after = project_dao.get(project_id)
    assert project_after is None, "Unshared project should be deleted"


@pytest.mark.anyio
async def test_member_removal_deletes_unshared_assistant(
    client: AsyncClient,
    dbsession,
):
    """Test that removing a member deletes assistants they created but never shared."""
    owner = await create_test_user(
        client,
        "unshared_asst_owner@test.com",
        hiring_approved=True,
    )
    member = await create_test_user(
        client,
        "unshared_asst_member@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Unshared Assistant Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Member creates an assistant with explicit access only for themselves
    assistant_dao = AssistantDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    assistant = assistant_dao.create_assistant(
        user_id=member["id"],
        first_name="Private",
        surname="Assistant",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.commit()
    agent_id = assistant.agent_id

    # Grant explicit access ONLY to the creator (making it private/unshared)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # Verify assistant exists
    assistant_before = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant_before is not None, "Assistant should exist"

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify assistant is deleted
    dbsession.expire_all()
    assistant_after = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant_after is None, "Unshared assistant should be deleted"


# =============================================================================
# Shared Resource Preservation Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_keeps_shared_project_with_user(
    client: AsyncClient,
    dbsession,
):
    """Test that removing a member keeps projects shared with another user."""
    owner = await create_test_user(client, "shared_user_owner@test.com")
    member = await create_test_user(client, "shared_user_member@test.com")
    other = await create_test_user(client, "shared_user_other@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Shared User Project Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add members
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": other["id"]},
        headers=owner["headers"],
    )

    # Member creates a project and shares it with "other"
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Shared Project",
        user_id=member["id"],
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Shared Project")
    project = projects[0][0]
    project_id = project.id

    # Grant access to creator
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        "project",
        project_id,
        owner_role.id,
        "user",
        member["id"],
    )
    # Also grant access to "other" user (making it shared)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        "project",
        project_id,
        viewer_role.id,
        "user",
        other["id"],
    )
    dbsession.commit()

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify project is NOT deleted (was shared)
    dbsession.expire_all()
    project_after = project_dao.get(project_id)
    assert project_after is not None, "Shared project should be kept"

    # Verify creator's access was revoked but other's access remains
    creator_access = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.grantee_id == member["id"],
            ResourceAccess.resource_id == project_id,
        )
        .first()
    )
    assert creator_access is None, "Creator's access should be revoked"

    other_access = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.grantee_id == other["id"],
            ResourceAccess.resource_id == project_id,
        )
        .first()
    )
    assert other_access is not None, "Other user's access should remain"


@pytest.mark.anyio
async def test_member_removal_keeps_shared_project_with_team(
    client: AsyncClient,
    dbsession,
):
    """Test that removing a member keeps projects shared with a team."""
    owner = await create_test_user(client, "shared_team_owner@test.com")
    member = await create_test_user(client, "shared_team_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Shared Team Project Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create a team
    team_dao = TeamDAO(dbsession)
    team = team_dao.create(name="Engineering", organization_id=org_id)
    dbsession.commit()

    # Member creates a project and shares it with the team
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    project_dao.create(
        name="Team Shared Project",
        user_id=member["id"],
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(organization_id=org_id, name="Team Shared Project")
    project = projects[0][0]
    project_id = project.id

    # Grant access to creator
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        "project",
        project_id,
        owner_role.id,
        "user",
        member["id"],
    )
    # Also grant access to the team (making it shared)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        "project",
        project_id,
        viewer_role.id,
        "team",
        str(team.id),
    )
    dbsession.commit()

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify project is NOT deleted (was shared with team)
    dbsession.expire_all()
    project_after = project_dao.get(project_id)
    assert project_after is not None, "Team-shared project should be kept"


@pytest.mark.anyio
async def test_member_removal_keeps_project_with_implicit_org_access(
    client: AsyncClient,
    dbsession,
):
    """Test that removing a member keeps projects with no explicit grants (implicit org access)."""
    owner = await create_test_user(client, "implicit_access_owner@test.com")
    member = await create_test_user(client, "implicit_access_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Implicit Access Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Member creates a project with NO explicit grants (uses implicit org access)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(dbsession)

    project_dao.create(
        name="Implicit Access Project",
        user_id=member["id"],
        organization_id=org_id,
    )
    dbsession.commit()
    projects = project_dao.filter(
        organization_id=org_id,
        name="Implicit Access Project",
    )
    project = projects[0][0]
    project_id = project.id

    # Verify there are NO explicit grants
    access_entries = resource_access_dao.get_resource_access("project", project_id)
    assert len(access_entries) == 0, "Should have no explicit grants"

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify project is NOT deleted (uses implicit org access = shared with all)
    dbsession.expire_all()
    project_after = project_dao.get(project_id)
    assert project_after is not None, "Implicitly shared project should be kept"


# =============================================================================
# Edge Cases
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_handles_no_resources(client: AsyncClient, dbsession):
    """Test that member removal works when member has no resources."""
    owner = await create_test_user(client, "no_resources_owner@test.com")
    member = await create_test_user(client, "no_resources_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "No Resources Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Remove member (should work fine with no resources to clean up)
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.anyio
async def test_member_removal_handles_multiple_unshared_resources(
    client: AsyncClient,
    dbsession,
):
    """Test that member removal deletes multiple unshared resources."""
    owner = await create_test_user(
        client,
        "multi_unshared_owner@test.com",
        hiring_approved=True,
    )
    member = await create_test_user(
        client,
        "multi_unshared_member@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Multi Unshared Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create multiple unshared projects and assistants
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    assistant_dao = AssistantDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    project_ids = []
    assistant_ids = []

    # Create 3 unshared projects
    for i in range(3):
        project_dao.create(
            name=f"Private Project {i}",
            user_id=member["id"],
            organization_id=org_id,
        )
        dbsession.flush()
        projects = project_dao.filter(
            organization_id=org_id,
            name=f"Private Project {i}",
        )
        project = projects[0][0]
        project_ids.append(project.id)
        resource_access_dao.grant_access(
            "project",
            project.id,
            owner_role.id,
            "user",
            member["id"],
        )

    # Create 2 unshared assistants
    for i in range(2):
        assistant = assistant_dao.create_assistant(
            user_id=member["id"],
            first_name=f"Private {i}",
            surname="Assistant",
            age=None,
            nationality=None,
            about=None,
            weekly_limit=None,
            max_parallel=None,
            organization_id=org_id,
        )
        dbsession.flush()
        assistant_ids.append(assistant.agent_id)
        resource_access_dao.grant_access(
            "assistant",
            assistant.agent_id,
            owner_role.id,
            "user",
            member["id"],
        )

    dbsession.commit()

    # Verify all resources exist
    for pid in project_ids:
        assert project_dao.get(pid) is not None
    for aid in assistant_ids:
        assert assistant_dao.get_assistant_by_agent_id(aid) is not None

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify all resources are deleted
    dbsession.expire_all()
    for pid in project_ids:
        assert project_dao.get(pid) is None, f"Project {pid} should be deleted"
    for aid in assistant_ids:
        assert (
            assistant_dao.get_assistant_by_agent_id(aid) is None
        ), f"Assistant {aid} should be deleted"


# =============================================================================
# Assistant Log Cleanup Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_deletes_assistant_logs(client: AsyncClient, dbsession):
    """Test that when an unshared assistant is deleted, its logs are also deleted."""
    owner = await create_test_user(
        client,
        "log_cleanup_owner@test.com",
        hiring_approved=True,
    )
    member = await create_test_user(
        client,
        "log_cleanup_member@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Log Cleanup Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Add member
    add_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    member_org_key = add_resp.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    # Create Assistants project for org
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )

    # Member creates assistant (unshared - only they have access)
    assistant_dao = AssistantDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    assistant = assistant_dao.create_assistant(
        user_id=member["id"],
        first_name="MemberOnly",
        surname="Bot",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.flush()
    agent_id = assistant.agent_id

    # Grant only the member Owner role (making it unshared)
    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # Create logs for this assistant in various contexts
    # Assistant-specific context
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "MemberOnlyBot/Tasks",
            "entries": [{"task": "Test task", "_assistant_id": str(agent_id)}],
        },
        headers=org_headers,
    )

    # Shared All/Contacts context
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {
                    "_assistant": "MemberOnlyBot",
                    "_assistant_id": str(agent_id),
                    "contact_id": 0,
                },
            ],
        },
        headers=org_headers,
    )

    # Verify logs exist
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=MemberOnlyBot/Tasks",
        headers=org_headers,
    )
    assert logs_resp.status_code == 200
    assert len(logs_resp.json()["logs"]) == 1

    contacts_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert contacts_resp.status_code == 200
    assistant_contact = [
        log
        for log in contacts_resp.json()["logs"]
        if log["entries"].get("_assistant_id") == str(agent_id)
    ]
    assert len(assistant_contact) == 1

    # Remove member - should delete assistant AND its logs
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify assistant is deleted
    dbsession.expire_all()
    assert assistant_dao.get_assistant_by_agent_id(agent_id) is None

    # Verify assistant-specific context logs are deleted
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=MemberOnlyBot/Tasks",
        headers=org_headers,
    )
    # Context may still exist but should have no logs, or context is deleted
    if logs_resp.status_code == 200:
        assert len(logs_resp.json().get("logs", [])) == 0

    # Verify logs in All/Contacts for this assistant are deleted
    contacts_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert contacts_resp.status_code == 200
    remaining_assistant_logs = [
        log
        for log in contacts_resp.json()["logs"]
        if log["entries"].get("_assistant_id") == str(agent_id)
    ]
    assert len(remaining_assistant_logs) == 0


# =============================================================================
# Contact is_system Update Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_sets_contact_is_system_false(
    client: AsyncClient,
    dbsession,
):
    """Test that removing a member sets their Contact log to is_system=False."""
    owner = await create_test_user(
        client,
        "contact_update_owner@test.com",
        hiring_approved=True,
    )
    member = await create_test_user(
        client,
        "contact_update_member@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Contact Update Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create Assistants project with All/Contacts context
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )

    # Create Contact log for the member with is_system=True
    # Use "Test" as first_name since create_test_user uses "Test" as name
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {
                    "first_name": "Test",  # Matches member's name from create_test_user
                    "surname": None,  # create_test_user doesn't set last_name
                    "is_system": True,
                    "contact_id": 1,
                    "timezone": "UTC",
                },
            ],
        },
        headers=org_headers,
    )

    # Verify Contact exists with is_system=True
    contacts_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert contacts_resp.status_code == 200
    member_contact = next(
        (
            log
            for log in contacts_resp.json()["logs"]
            if log["entries"].get("first_name") == "Test"
            and log["entries"].get("is_system") is True
        ),
        None,
    )
    assert (
        member_contact is not None
    ), "Member's Contact should exist with is_system=True"

    # Remove member
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify Contact now has is_system=False
    contacts_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert contacts_resp.status_code == 200

    # Find the member's contact - should now have is_system=False
    member_contact = next(
        (
            log
            for log in contacts_resp.json()["logs"]
            if log["entries"].get("first_name") == "Test"
        ),
        None,
    )
    assert member_contact is not None, "Member's Contact should still exist"
    assert (
        member_contact["entries"].get("is_system") is False
    ), "is_system should be False after member removal"
