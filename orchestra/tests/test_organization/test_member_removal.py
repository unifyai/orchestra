"""Tests for member removal cleanup behavior.

When a member is removed from an organization, the following should happen:
1. ResourceAccess entries for the user are revoked
2. TeamMember entries are removed
3. Unshared resources created by the user are deleted
4. Shared resources created by the user are kept
"""

from unittest.mock import AsyncMock, MagicMock, patch

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
from orchestra.db.models.orchestra_models import (
    AssistantCleanupTask,
    AssistantContact,
    ResourceAccess,
    TeamMember,
)
from orchestra.tests.utils import create_test_user


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Automatically mock assistant infrastructure webhooks and BucketService for all tests."""
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken, patch(
        "orchestra.web.api.assistant.views.process_assistant_cleanup_tasks",
        new_callable=AsyncMock,
    ) as mock_assistant_cleanup, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings, patch(
        "orchestra.web.api.organization.views.process_assistant_cleanup_tasks",
        new_callable=AsyncMock,
    ) as mock_org_cleanup, patch(
        "orchestra.web.api.organization.views.BucketService",
    ) as mock_bucket_cls:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_assistant_cleanup.return_value = {
            "processed": 1,
            "completed": 1,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_org_cleanup.return_value = {
            "processed": 1,
            "completed": 1,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_settings.is_staging = True

        mock_bucket_instance = MagicMock()
        mock_bucket_instance.delete_all_assistant_data.return_value = {
            "media_files": 0,
            "call_recordings": 0,
            "message_attachments": 0,
        }
        mock_bucket_cls.return_value = mock_bucket_instance

        yield (
            mock_wake_up,
            mock_reawaken,
            mock_assistant_cleanup,
            mock_org_cleanup,
            mock_bucket_cls,
        )


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


@pytest.mark.anyio
async def test_member_removal_rolls_back_if_pool_cleanup_fails(
    client_concurrent: AsyncClient,
):
    """Member removal should stay atomic until shared-pool cleanup succeeds."""

    owner = await create_test_user(client_concurrent, "rollback_owner@test.com")
    member = await create_test_user(client_concurrent, "rollback_member@test.com")

    org_resp = await client_concurrent.post(
        "/v0/organizations",
        json={"name": "Rollback Cleanup Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    add_member_resp = await client_concurrent.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_resp.status_code == status.HTTP_201_CREATED

    with patch(
        "orchestra.db.dao.shared_pool_dao.SharedPoolDAO.cleanup_departed_member_routes",
        side_effect=RuntimeError("simulated shared-pool cleanup failure"),
    ):
        remove_resp = await client_concurrent.delete(
            f"/v0/organizations/{org_id}/members/{member['id']}",
            headers=owner["headers"],
        )

    assert remove_resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert remove_resp.json()["detail"] == "Failed to remove member"

    members_resp = await client_concurrent.get(
        f"/v0/organizations/{org_id}/members",
        headers=owner["headers"],
    )
    assert members_resp.status_code == status.HTTP_200_OK
    member_ids = {org_member["user_id"] for org_member in members_resp.json()}
    assert member["id"] in member_ids


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
    )
    member = await create_test_user(
        client,
        "unshared_asst_member@test.com",
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
    with patch(
        "orchestra.web.api.organization.views.process_assistant_cleanup_tasks",
        new_callable=AsyncMock,
    ) as mock_cleanup:
        mock_cleanup.return_value = {
            "processed": 1,
            "completed": 1,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        remove_resp = await client.delete(
            f"/v0/organizations/{org_id}/members/{member['id']}",
            headers=owner["headers"],
        )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify assistant is deleted
    dbsession.expire_all()
    assistant_after = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant_after is None, "Unshared assistant should be deleted"
    mock_cleanup.assert_awaited_once()


@pytest.mark.anyio
async def test_member_removal_deprovisions_contacts_before_deleting_unshared_assistant(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "contact_cleanup_owner@test.com")
    member = await create_test_user(client, "contact_cleanup_member@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Contact Cleanup Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    assistant_dao = AssistantDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    assistant = assistant_dao.create_assistant(
        user_id=member["id"],
        first_name="Cleanup",
        surname="Target",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.flush()
    agent_id = assistant.agent_id

    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )

    dbsession.add_all(
        [
            AssistantContact(
                assistant_id=agent_id,
                contact_type="phone",
                contact_value="+15551110000",
            ),
            AssistantContact(
                assistant_id=agent_id,
                contact_type="email",
                contact_value="cleanup-member@assistant.unify.ai",
            ),
            AssistantContact(
                assistant_id=agent_id,
                contact_type="whatsapp",
                contact_value="+15552220000",
            ),
        ],
    )
    dbsession.commit()

    with patch(
        "orchestra.services.assistant_cleanup_service.delete_phone_number",
        new_callable=AsyncMock,
    ) as mock_delete_phone, patch(
        "orchestra.services.assistant_cleanup_service.delete_email",
        new_callable=AsyncMock,
    ) as mock_delete_email, patch(
        "orchestra.db.dao.shared_pool_dao.SharedPoolDAO.delete_routes_for_assistant",
        return_value=1,
    ) as mock_delete_routes:
        remove_resp = await client.delete(
            f"/v0/organizations/{org_id}/members/{member['id']}",
            headers=owner["headers"],
        )

    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT
    mock_delete_phone.assert_awaited_once_with("+15551110000", deploy_env=None)
    mock_delete_email.assert_awaited_once_with(
        "cleanup-member@assistant.unify.ai",
        deploy_env=None,
    )
    mock_delete_routes.assert_called_once_with(agent_id)

    dbsession.expire_all()
    cleanup_task = (
        dbsession.query(AssistantCleanupTask)
        .filter(AssistantCleanupTask.assistant_id == agent_id)
        .one()
    )
    assert cleanup_task.status == "pending"


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
    )
    member = await create_test_user(
        client,
        "multi_unshared_member@test.com",
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
    """
    Test that when an unshared assistant is deleted, its logs are also deleted.

    Uses 3-tier context hierarchy:
    - Tier 1: All/Transcripts (global aggregate)
    - Tier 2: user_id/All/Transcripts (user aggregate)
    - Tier 3: user_id/assistant_id/Transcripts (user + assistant specific)

    When member is removed and their unshared assistant is deleted:
    - Assistant-specific contexts (Tier 3) should be deleted
    - Tier 2 user aggregates should be cleaned via sibling cleanup
    - Tier 1 All/* should remain as the protected archive
    """
    owner = await create_test_user(
        client,
        "log_cleanup_owner@test.com",
    )
    member = await create_test_user(
        client,
        "log_cleanup_member@test.com",
    )
    user_name = "test-user"

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
    assistant_name = str(agent_id)

    # Grant only the member Owner role (making it unshared)
    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # Define 3-tier context names
    tier3_context = f"{user_name}/{assistant_name}/Transcripts"
    tier2_context = f"{user_name}/All/Transcripts"
    tier1_context = "All/Transcripts"

    # Create log in Tier 3 (assistant-specific) context with _user and _assistant fields
    log_resp = await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": tier3_context,
            "entries": [
                {
                    "task": "Test task",
                    "_user": user_name,
                    "_assistant": assistant_name,
                    "_assistant_id": str(agent_id),
                },
            ],
        },
        headers=org_headers,
    )
    assert log_resp.status_code == 200
    log_id = log_resp.json()["log_event_ids"][0]

    # Add the same log to Tier 1 and Tier 2 contexts
    for ctx in [tier1_context, tier2_context]:
        add_resp = await client.post(
            "/v0/project/Assistants/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=org_headers,
        )
        assert add_resp.status_code == 200

    # Verify log exists in all three contexts
    for ctx in [tier1_context, tier2_context, tier3_context]:
        logs_resp = await client.get(
            f"/v0/logs?project_name=Assistants&context={ctx}",
            headers=org_headers,
        )
        assert logs_resp.status_code == 200
        assert log_id in [
            log["id"] for log in logs_resp.json()["logs"]
        ], f"Log should exist in {ctx}"

    # Remove member - should delete the assistant and clear lower-tier copies.
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify assistant is deleted
    dbsession.expire_all()
    assert assistant_dao.get_assistant_by_agent_id(agent_id) is None

    # Verify log is removed from tier2 and tier3 contexts via sibling cleanup
    for ctx in [tier2_context, tier3_context]:
        logs_resp = await client.get(
            f"/v0/logs?project_name=Assistants&context={ctx}",
            headers=org_headers,
        )
        if logs_resp.status_code == 200:
            assert log_id not in [
                log["id"] for log in logs_resp.json()["logs"]
            ], f"Log should be cleaned from {ctx}"

    # Archive protection: log remains in topmost All/* context for historical record
    logs_resp = await client.get(
        f"/v0/logs?project_name=Assistants&context={tier1_context}",
        headers=org_headers,
    )
    assert logs_resp.status_code == 200
    assert log_id in [
        log["id"] for log in logs_resp.json()["logs"]
    ], f"Log should remain in archive {tier1_context}"


@pytest.mark.anyio
async def test_member_removal_preserves_other_assistant_logs(
    client: AsyncClient,
    dbsession,
):
    """
    Test that logs from OTHER assistants in shared contexts are preserved.

    When member A is removed and their assistant is deleted:
    - Member A's assistant logs should be removed from Tier 2 and Tier 3
    - Member A's topmost All/* archive copy should remain
    - Member B's assistant logs in shared All/* contexts should NOT be affected
    """
    owner = await create_test_user(
        client,
        "preserve_owner@test.com",
    )
    member_a = await create_test_user(
        client,
        "preserve_member_a@test.com",
    )
    member_b = await create_test_user(
        client,
        "preserve_member_b@test.com",
    )
    user_name = "preserve-user"

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Preserve Logs Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Add both members
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member_a["id"]},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member_b["id"]},
        headers=owner["headers"],
    )

    # Create Assistants project
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )

    # Create two assistants - one for each member (both unshared)
    assistant_dao = AssistantDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    # Member A's assistant
    assistant_a = assistant_dao.create_assistant(
        user_id=member_a["id"],
        first_name="AssistantA",
        surname="Remove",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.flush()
    agent_id_a = assistant_a.agent_id
    assistant_name_a = str(agent_id_a)

    resource_access_dao.grant_access(
        "assistant",
        agent_id_a,
        owner_role.id,
        "user",
        member_a["id"],
    )

    # Member B's assistant
    assistant_b = assistant_dao.create_assistant(
        user_id=member_b["id"],
        first_name="AssistantB",
        surname="Keep",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.flush()
    agent_id_b = assistant_b.agent_id
    assistant_name_b = str(agent_id_b)

    resource_access_dao.grant_access(
        "assistant",
        agent_id_b,
        owner_role.id,
        "user",
        member_b["id"],
    )
    dbsession.commit()

    # Define contexts
    tier3_a = f"{user_name}/{assistant_name_a}/Transcripts"
    tier3_b = f"{user_name}/{assistant_name_b}/Transcripts"
    tier2_context = f"{user_name}/All/Transcripts"
    tier1_context = "All/Transcripts"

    # Create log for Assistant A
    log_resp_a = await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": tier3_a,
            "entries": [
                {
                    "message": "Log from Assistant A",
                    "_user": user_name,
                    "_assistant": assistant_name_a,
                    "_assistant_id": str(agent_id_a),
                },
            ],
        },
        headers=org_headers,
    )
    assert log_resp_a.status_code == 200
    log_id_a = log_resp_a.json()["log_event_ids"][0]

    # Create log for Assistant B
    log_resp_b = await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": tier3_b,
            "entries": [
                {
                    "message": "Log from Assistant B",
                    "_user": user_name,
                    "_assistant": assistant_name_b,
                    "_assistant_id": str(agent_id_b),
                },
            ],
        },
        headers=org_headers,
    )
    assert log_resp_b.status_code == 200
    log_id_b = log_resp_b.json()["log_event_ids"][0]

    # Add both logs to shared contexts
    for log_id in [log_id_a, log_id_b]:
        for ctx in [tier1_context, tier2_context]:
            add_resp = await client.post(
                "/v0/project/Assistants/contexts/add_logs",
                json={"context_name": ctx, "log_ids": [log_id]},
                headers=org_headers,
            )
            assert add_resp.status_code == 200

    # Verify both logs exist in shared contexts
    for ctx in [tier1_context, tier2_context]:
        logs_resp = await client.get(
            f"/v0/logs?project_name=Assistants&context={ctx}",
            headers=org_headers,
        )
        assert logs_resp.status_code == 200
        log_ids = [log["id"] for log in logs_resp.json()["logs"]]
        assert log_id_a in log_ids, f"Log A should exist in {ctx}"
        assert log_id_b in log_ids, f"Log B should exist in {ctx}"

    # Remove member A - should delete assistant A and its logs only
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member_a['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify Assistant A is deleted
    dbsession.expire_all()
    assert assistant_dao.get_assistant_by_agent_id(agent_id_a) is None

    # Verify Assistant B still exists
    assert assistant_dao.get_assistant_by_agent_id(agent_id_b) is not None

    # Verify log A is removed from tier2 (User/All/*) but remains in tier1 (All/*)
    # due to archive protection - topmost All/* contexts are preserved as historical records
    logs_resp = await client.get(
        f"/v0/logs?project_name=Assistants&context={tier2_context}",
        headers=org_headers,
    )
    assert logs_resp.status_code == 200
    log_ids = [log["id"] for log in logs_resp.json()["logs"]]
    assert log_id_a not in log_ids, f"Log A should be removed from {tier2_context}"
    assert log_id_b in log_ids, f"Log B should still exist in {tier2_context}"

    # Archive protection: log A remains in topmost All/* context for historical record
    logs_resp = await client.get(
        f"/v0/logs?project_name=Assistants&context={tier1_context}",
        headers=org_headers,
    )
    assert logs_resp.status_code == 200
    log_ids = [log["id"] for log in logs_resp.json()["logs"]]
    assert log_id_a in log_ids, f"Log A should remain in archive {tier1_context}"
    assert log_id_b in log_ids, f"Log B should still exist in {tier1_context}"

    # Verify Assistant B's Tier 3 context is untouched
    logs_resp_b = await client.get(
        f"/v0/logs?project_name=Assistants&context={tier3_b}",
        headers=org_headers,
    )
    assert logs_resp_b.status_code == 200
    assert log_id_b in [log["id"] for log in logs_resp_b.json()["logs"]]


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
    )
    member = await create_test_user(
        client,
        "contact_update_member@test.com",
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
    # Match by email since contact sync now uses email
    await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {
                    "email_address": member["email"],  # Matches member's email
                    "first_name": "Test",
                    "surname": None,
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
        "/v0/logs?project_name=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert contacts_resp.status_code == 200
    member_contact = next(
        (
            log
            for log in contacts_resp.json()["logs"]
            if log["entries"].get("email_address") == member["email"]
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
        "/v0/logs?project_name=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert contacts_resp.status_code == 200

    # Find the member's contact - should now have is_system=False
    member_contact = next(
        (
            log
            for log in contacts_resp.json()["logs"]
            if log["entries"].get("email_address") == member["email"]
        ),
        None,
    )
    assert member_contact is not None, "Member's Contact should still exist"
    assert (
        member_contact["entries"].get("is_system") is False
    ), "is_system should be False after member removal"


# =============================================================================
# Self-Removal Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_can_remove_themselves_from_org(client: AsyncClient, dbsession):
    """Test that a member can remove themselves from an organization (leave)."""
    owner = await create_test_user(client, "self_removal_owner@test.com")
    member = await create_test_user(client, "self_removal_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Self Removal Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    add_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == status.HTTP_201_CREATED
    member_org_key = add_resp.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    # Verify member exists in org
    org_member_dao = OrganizationMemberDAO(dbsession)
    membership = org_member_dao.get_member(member["id"], org_id)
    assert membership is not None, "Member should exist in org"

    # Member removes themselves (self-removal / leave org)
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=member["headers"],  # Using member's personal API key
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify member no longer exists in org
    dbsession.expire_all()
    membership = org_member_dao.get_member(member["id"], org_id)
    assert membership is None, "Member should be removed from org"


@pytest.mark.anyio
async def test_member_cannot_remove_other_members(client: AsyncClient, dbsession):
    """Test that a member cannot remove other members (only self-removal allowed)."""
    owner = await create_test_user(client, "member_remove_owner@test.com")
    member1 = await create_test_user(client, "member_remove_member1@test.com")
    member2 = await create_test_user(client, "member_remove_member2@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Member Remove Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add both members
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member1["id"]},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member2["id"]},
        headers=owner["headers"],
    )

    # Member1 tries to remove member2 - should fail
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member2['id']}",
        headers=member1["headers"],
    )
    assert remove_resp.status_code == status.HTTP_403_FORBIDDEN
    assert "permission" in remove_resp.json()["detail"].lower()

    # Verify member2 still exists in org
    org_member_dao = OrganizationMemberDAO(dbsession)
    membership = org_member_dao.get_member(member2["id"], org_id)
    assert membership is not None, "Member2 should still exist in org"


@pytest.mark.anyio
async def test_owner_cannot_remove_themselves(client: AsyncClient, dbsession):
    """Test that the owner cannot remove themselves from the organization."""
    owner = await create_test_user(client, "owner_self_remove@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Owner Self Remove Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Owner tries to remove themselves - should fail
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{owner['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "owner" in remove_resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_admin_can_remove_other_members(client: AsyncClient, dbsession):
    """Test that an admin (with org:write) can remove other members."""
    owner = await create_test_user(client, "admin_remove_owner@test.com")
    admin = await create_test_user(client, "admin_remove_admin@test.com")
    member = await create_test_user(client, "admin_remove_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Admin Remove Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Get Admin role
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    assert admin_role is not None

    # Add admin with Admin role
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin["id"], "role_id": admin_role.id},
        headers=owner["headers"],
    )

    # Add member with default Member role
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Admin removes member - should succeed
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=admin["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify member is gone
    org_member_dao = OrganizationMemberDAO(dbsession)
    dbsession.expire_all()
    membership = org_member_dao.get_member(member["id"], org_id)
    assert membership is None, "Member should be removed from org"


# =============================================================================
# GCS Cleanup Tests
# =============================================================================


@pytest.mark.anyio
async def test_member_removal_cleans_up_gcs_for_deleted_assistants(
    client: AsyncClient,
    dbsession,
    mock_assistant_infra_calls,
):
    """Deleted assistants leave GCS cleanup to the durable cleanup task path."""
    _, _, _, mock_org_cleanup, mock_bucket_cls = mock_assistant_infra_calls
    mock_bucket_instance = mock_bucket_cls.return_value

    owner = await create_test_user(client, "gcs_cleanup_owner@test.com")
    member = await create_test_user(client, "gcs_cleanup_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "GCS Cleanup Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Member creates an unshared assistant
    assistant_dao = AssistantDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    assistant = assistant_dao.create_assistant(
        user_id=member["id"],
        first_name="GCSCleanup",
        surname="Assistant",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.flush()
    agent_id = assistant.agent_id

    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # Remove member - triggers assistant deletion and durable cleanup
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    # Verify assistant is deleted from DB
    dbsession.expire_all()
    assert assistant_dao.get_assistant_by_agent_id(agent_id) is None

    mock_org_cleanup.assert_awaited_once()
    mock_bucket_instance.delete_all_assistant_data.assert_not_called()


@pytest.mark.anyio
async def test_member_removal_no_gcs_cleanup_for_shared_assistants(
    client: AsyncClient,
    dbsession,
    mock_assistant_infra_calls,
):
    """Test that GCS data is NOT cleaned up for assistants that survive member removal (shared)."""
    _, _, _, _, mock_bucket_cls = mock_assistant_infra_calls
    mock_bucket_instance = mock_bucket_cls.return_value

    owner = await create_test_user(client, "gcs_shared_owner@test.com")
    member = await create_test_user(client, "gcs_shared_member@test.com")
    other = await create_test_user(client, "gcs_shared_other@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "GCS Shared Org"},
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

    # Member creates a SHARED assistant (shared with 'other')
    assistant_dao = AssistantDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    assistant = assistant_dao.create_assistant(
        user_id=member["id"],
        first_name="Shared",
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

    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )
    resource_access_dao.grant_access(
        "assistant",
        agent_id,
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

    # Verify assistant still exists (was shared)
    dbsession.expire_all()
    assert assistant_dao.get_assistant_by_agent_id(agent_id) is not None

    # Verify delete_all_assistant_data was NOT called (assistant survived)
    mock_bucket_instance.delete_all_assistant_data.assert_not_called()


@pytest.mark.anyio
async def test_member_removal_gcs_failure_does_not_block(
    client: AsyncClient,
    dbsession,
    mock_assistant_infra_calls,
):
    """Durable cleanup retries do not block member removal responses."""
    _, _, _, mock_org_cleanup, mock_bucket_cls = mock_assistant_infra_calls
    mock_bucket_instance = mock_bucket_cls.return_value

    owner = await create_test_user(client, "gcs_fail_owner@test.com")
    member = await create_test_user(client, "gcs_fail_member@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "GCS Fail Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Member creates an unshared assistant
    assistant_dao = AssistantDAO(dbsession)
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    assistant = assistant_dao.create_assistant(
        user_id=member["id"],
        first_name="FailTest",
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

    resource_access_dao.grant_access(
        "assistant",
        agent_id,
        owner_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # Remove member - cleanup retries remain asynchronous and must not block.
    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT
    mock_org_cleanup.assert_awaited_once()
    mock_bucket_instance.delete_all_assistant_data.assert_not_called()

    # DB deletion should still have occurred
    dbsession.expire_all()
    assert assistant_dao.get_assistant_by_agent_id(agent_id) is None
