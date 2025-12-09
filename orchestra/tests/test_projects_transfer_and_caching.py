"""
Phase 6 Tests: Project Transfer & Permission Caching.

Tests for:
1. Personal → Organization project transfer
2. Organization → Personal project transfer
3. Permission caching performance
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.models.orchestra_models import ResourceAccess
from orchestra.tests.utils import create_test_user

# ==================== Project Transfer Tests ====================


@pytest.mark.anyio
async def test_transfer_personal_to_organization(client: AsyncClient, dbsession):
    """Test transferring a personal project to an organization."""
    user = await create_test_user(client, "transfer_personal_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Transfer Test Org"},
        headers=user["headers"],
    )
    org_id = org_response.json()["id"]

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Personal_Transfer_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="Personal_Transfer_Project")
    project = projects[0][0]

    # Verify it's personal
    assert project.user_id == user["id"]
    assert project.organization_id is None

    # Transfer to organization
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_200_OK
    transfer_data = transfer_response.json()

    assert transfer_data["success"] is True
    assert transfer_data["project_id"] == project.id
    assert transfer_data["from_type"] == "personal"
    assert transfer_data["to_type"] == "organization"

    # Verify project is now organizational
    dbsession.refresh(project)
    assert project.organization_id == org_id
    assert project.user_id is None

    # Verify explicit Owner grant was created for the transferring user
    resource_access_dao = ResourceAccessDAO(dbsession)
    access_entries = resource_access_dao.get_resource_access("project", project.id)
    assert len(access_entries) == 1
    assert access_entries[0].grantee_type == "user"
    assert access_entries[0].grantee_id == user["id"]


@pytest.mark.anyio
async def test_transfer_organization_to_personal(client: AsyncClient, dbsession):
    """Test transferring an organizational project to personal ownership."""
    owner = await create_test_user(client, "transfer_org_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Org Transfer Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create org project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Org_Transfer_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="Org_Transfer_Project")
    project = projects[0][0]

    # Verify it's organizational
    assert project.organization_id == org_id
    assert project.user_id is None

    # Transfer to personal
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-personal",
        headers=owner["headers"],
    )

    assert transfer_response.status_code == status.HTTP_200_OK
    transfer_data = transfer_response.json()

    assert transfer_data["success"] is True
    assert transfer_data["project_id"] == project.id
    assert transfer_data["from_type"] == "organization"
    assert transfer_data["to_type"] == "personal"
    assert "team shares have been removed" in transfer_data["message"]

    # Verify project is now personal
    dbsession.refresh(project)
    assert project.user_id == owner["id"]
    assert project.organization_id is None

    # Verify ResourceAccess entries were deleted
    resource_access_dao = ResourceAccessDAO(dbsession)
    access_entries = resource_access_dao.get_resource_access("project", project.id)
    assert len(access_entries) == 0


@pytest.mark.anyio
async def test_cannot_transfer_already_org_project(client: AsyncClient, dbsession):
    """Test that transferring an already-org project to org fails."""
    owner = await create_test_user(client, "double_transfer_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Double Transfer Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create org project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Already_Org_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="Already_Org_Project")
    project = projects[0][0]

    # Try to transfer already-org project to org
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=owner["headers"],
    )

    assert transfer_response.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        "already associated with an organization" in transfer_response.json()["detail"]
    )


@pytest.mark.anyio
async def test_cannot_transfer_already_personal_project(client: AsyncClient, dbsession):
    """Test that transferring an already-personal project to personal fails."""
    user = await create_test_user(client, "already_personal@test.com")

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Already_Personal_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="Already_Personal_Project")
    project = projects[0][0]

    # Try to transfer already-personal project to personal
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-personal",
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already personal" in transfer_response.json()["detail"]


@pytest.mark.anyio
async def test_transfer_requires_ownership(client: AsyncClient, dbsession):
    """Test that transferring a personal project requires ownership."""
    owner = await create_test_user(client, "project_owner@test.com")
    other_user = await create_test_user(client, "not_owner@test.com")

    # Create personal project as owner
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Ownership_Test_Project",
        user_id=owner["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=owner["id"], name="Ownership_Test_Project")
    project = projects[0][0]

    # Other user creates an org
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Other User Org"},
        headers=other_user["headers"],
    )
    org_id = org_response.json()["id"]

    # Other user tries to transfer owner's project
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=other_user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_403_FORBIDDEN
    assert "do not own" in transfer_response.json()["detail"]


@pytest.mark.anyio
async def test_transfer_requires_project_write_permission_in_org(
    client: AsyncClient,
    dbsession,
):
    """Test that transferring to org requires user to be member with project:write permission."""
    user = await create_test_user(client, "transfer_user@test.com")
    org_owner = await create_test_user(client, "org_owner@test.com")

    # Org owner creates organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Restricted Org"},
        headers=org_owner["headers"],
    )
    org_id = org_response.json()["id"]

    # User creates personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="User_Personal_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="User_Personal_Project")
    project = projects[0][0]

    # User tries to transfer to org they're not a member of
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_403_FORBIDDEN
    assert "do not have permission" in transfer_response.json()["detail"]
    assert "project:write" in transfer_response.json()["detail"]


@pytest.mark.anyio
async def test_viewer_cannot_transfer_but_member_can(client: AsyncClient, dbsession):
    """Test that Viewer role (no project:write) can't transfer, but Member role (has project:write) can."""
    user = await create_test_user(client, "member_user@test.com")
    org_owner = await create_test_user(client, "org_owner_2@test.com")

    # Org owner creates organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Role Test Org"},
        headers=org_owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add user to org as Viewer (has project:read only, not project:write)
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    add_member_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user["id"], "level": "user", "role_id": viewer_role.id},
        headers=org_owner["headers"],
    )
    assert add_member_response.status_code == status.HTTP_201_CREATED

    # User creates personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Viewer_Test_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="Viewer_Test_Project")
    project = projects[0][0]

    # User tries to transfer as Viewer (should fail - no project:write)
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_403_FORBIDDEN
    assert "project:write" in transfer_response.json()["detail"]

    # Now update user's role to Member (has project:write)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    update_role_response = await client.patch(
        f"/v0/organizations/{org_id}/members/{user['id']}/role",
        json={"role_id": member_role.id},
        headers=org_owner["headers"],
    )
    assert update_role_response.status_code == status.HTTP_200_OK

    # Now user should be able to transfer (has project:write)
    transfer_response_2 = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )

    assert transfer_response_2.status_code == status.HTTP_200_OK
    transfer_data = transfer_response_2.json()
    assert transfer_data["success"] is True
    assert transfer_data["from_type"] == "personal"
    assert transfer_data["to_type"] == "organization"

    # Verify project is now organizational
    dbsession.refresh(project)
    assert project.organization_id == org_id
    assert project.user_id is None

    # Verify explicit Owner grant was created for the transferring user
    resource_access_dao = ResourceAccessDAO(dbsession)
    access_entries = resource_access_dao.get_resource_access("project", project.id)
    assert len(access_entries) == 1
    assert access_entries[0].grantee_type == "user"
    assert access_entries[0].grantee_id == user["id"]


@pytest.mark.anyio
async def test_org_to_personal_deletes_resource_access(client: AsyncClient, dbsession):
    """Test that org → personal transfer deletes all ResourceAccess entries."""
    owner = await create_test_user(client, "cleanup_owner@test.com")
    member = await create_test_user(client, "cleanup_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Cleanup Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "level": "user"},
        headers=owner["headers"],
    )

    # Create org project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Cleanup_Project",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    projects = project_dao.filter(organization_id=org_id, name="Cleanup_Project")
    project = projects[0][0]

    # Share with member (create explicit ResourceAccess entry)
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao = ResourceAccessDAO(dbsession)

    resource_access_dao.grant_access(
        resource_type="project",
        resource_id=project.id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.commit()

    # Verify ResourceAccess entry exists
    access_entries = resource_access_dao.get_resource_access("project", project.id)
    assert len(access_entries) >= 1

    # Transfer to personal
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-personal",
        headers=owner["headers"],
    )

    assert transfer_response.status_code == status.HTTP_200_OK

    # Verify ALL ResourceAccess entries are deleted
    dbsession.expire_all()  # Clear session cache
    access_entries_after = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.resource_type == "project",
            ResourceAccess.resource_id == project.id,
        )
        .all()
    )
    assert len(access_entries_after) == 0


# ==================== Name Conflict Tests ====================


@pytest.mark.anyio
async def test_transfer_to_org_fails_on_name_conflict(client: AsyncClient, dbsession):
    """Test that transferring to an org fails if project with same name exists."""
    user = await create_test_user(client, "name_conflict_org@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Name Conflict Test Org"},
        headers=user["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Create an org project with a specific name
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Duplicate_Name",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Create a personal project with the SAME name
    project_dao.create(
        name="Duplicate_Name",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    personal_projects = project_dao.filter(user_id=user["id"], name="Duplicate_Name")
    personal_project = personal_projects[0][0]

    # Try to transfer personal project to org - should fail with 409
    transfer_response = await client.post(
        f"/v0/project/{personal_project.id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_409_CONFLICT
    assert "already has a project named" in transfer_response.json()["detail"]


@pytest.mark.anyio
async def test_transfer_to_personal_fails_on_name_conflict(
    client: AsyncClient,
    dbsession,
):
    """Test that transferring to personal fails if user has project with same name."""
    user = await create_test_user(client, "name_conflict_personal@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Personal Conflict Test Org"},
        headers=user["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Create a personal project with a specific name
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Personal_Duplicate",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    # Create an org project with the SAME name
    project_dao.create(
        name="Personal_Duplicate",
        user_id=None,
        organization_id=org_id,
    )
    dbsession.commit()

    org_projects = project_dao.filter(organization_id=org_id, name="Personal_Duplicate")
    org_project = org_projects[0][0]

    # Try to transfer org project to personal - should fail with 409
    transfer_response = await client.post(
        f"/v0/project/{org_project.id}/transfer-to-personal",
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_409_CONFLICT
    assert "already have a personal project named" in transfer_response.json()["detail"]


@pytest.mark.anyio
async def test_transfer_to_nonexistent_org_returns_404(client: AsyncClient, dbsession):
    """Test that transferring to a non-existent organization returns 404."""
    user = await create_test_user(client, "nonexistent_org@test.com")

    # Create personal project
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    project_dao.create(
        name="Orphan_Project",
        user_id=user["id"],
        organization_id=None,
    )
    dbsession.commit()

    projects = project_dao.filter(user_id=user["id"], name="Orphan_Project")
    project = projects[0][0]

    # Try to transfer to non-existent org
    transfer_response = await client.post(
        f"/v0/project/{project.id}/transfer-to-organization",
        json={"organization_id": 999999},
        headers=user["headers"],
    )

    assert transfer_response.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in transfer_response.json()["detail"]


# ==================== Transfer Preservation Tests ====================


@pytest.mark.anyio
async def test_transfer_preserves_logs_and_contexts(client: AsyncClient, dbsession):
    """Test that logs and contexts remain associated after transfer."""
    user = await create_test_user(client, "preserve_logs@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Logs Preservation Org"},
        headers=user["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Create personal project
    project_response = await client.post(
        "/v0/project",
        json={"name": "Logs_Preserve_Project", "is_versioned": False},
        headers=user["headers"],
    )
    assert project_response.status_code == status.HTTP_200_OK

    # Add logs with a context
    log_response = await client.post(
        "/v0/logs",
        json={
            "project": "Logs_Preserve_Project",
            "context": "TestContext",
            "entries": [
                {"field1": "value1", "field2": 123},
                {"field1": "value2", "field2": 456},
            ],
        },
        headers=user["headers"],
    )
    assert log_response.status_code == status.HTTP_200_OK

    # Get project ID
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    projects = project_dao.filter(user_id=user["id"], name="Logs_Preserve_Project")
    project = projects[0][0]
    project_id = project.id

    # Verify logs exist before transfer
    query_response = await client.post(
        "/v0/logs/query",
        json={"project": "Logs_Preserve_Project", "context": "TestContext"},
        headers=user["headers"],
    )
    assert query_response.status_code == status.HTTP_200_OK
    logs_before = query_response.json()
    assert len(logs_before["logs"]) == 2

    # Transfer to organization
    transfer_response = await client.post(
        f"/v0/project/{project_id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )
    assert transfer_response.status_code == status.HTTP_200_OK

    # Verify project is now organizational
    dbsession.refresh(project)
    assert project.organization_id == org_id
    assert project.user_id is None

    # Verify logs are still queryable after transfer
    query_response_after = await client.post(
        "/v0/logs/query",
        json={"project": "Logs_Preserve_Project", "context": "TestContext"},
        headers=user["headers"],
    )
    assert query_response_after.status_code == status.HTTP_200_OK
    logs_after = query_response_after.json()
    assert len(logs_after["logs"]) == 2

    # Verify the data is the same
    values_before = {e.get("field1") for e in logs_before["logs"]}
    values_after = {e.get("field1") for e in logs_after["logs"]}
    assert values_before == values_after


@pytest.mark.anyio
async def test_transfer_preserves_field_types(client: AsyncClient, dbsession):
    """Test that field type definitions are preserved after transfer."""
    from orchestra.db.models.orchestra_models import FieldType

    user = await create_test_user(client, "preserve_fieldtypes@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "FieldTypes Preservation Org"},
        headers=user["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Create personal project
    project_response = await client.post(
        "/v0/project",
        json={"name": "FieldTypes_Project", "is_versioned": False},
        headers=user["headers"],
    )
    assert project_response.status_code == status.HTTP_200_OK

    # Add logs to create field types
    log_response = await client.post(
        "/v0/logs",
        json={
            "project": "FieldTypes_Project",
            "context": "TypedContext",
            "entries": [
                {"string_field": "hello", "int_field": 42, "float_field": 3.14},
            ],
        },
        headers=user["headers"],
    )
    assert log_response.status_code == status.HTTP_200_OK

    # Get project ID
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    projects = project_dao.filter(user_id=user["id"], name="FieldTypes_Project")
    project = projects[0][0]
    project_id = project.id

    # Count field types before transfer
    field_types_before = (
        dbsession.query(FieldType).filter(FieldType.project_id == project_id).all()
    )
    field_names_before = {ft.field_name for ft in field_types_before}

    # Transfer to organization
    transfer_response = await client.post(
        f"/v0/project/{project_id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )
    assert transfer_response.status_code == status.HTTP_200_OK

    # Verify field types still exist after transfer
    dbsession.expire_all()
    field_types_after = (
        dbsession.query(FieldType).filter(FieldType.project_id == project_id).all()
    )
    field_names_after = {ft.field_name for ft in field_types_after}

    assert field_names_before == field_names_after
    assert len(field_types_after) == len(field_types_before)


@pytest.mark.anyio
async def test_transfer_preserves_interfaces(client: AsyncClient, dbsession):
    """Test that interfaces are preserved after transfer."""
    from orchestra.db.models.orchestra_models import Interface

    user = await create_test_user(client, "preserve_interfaces@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Interfaces Preservation Org"},
        headers=user["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Create personal project
    project_response = await client.post(
        "/v0/project",
        json={"name": "Interfaces_Project", "is_versioned": False},
        headers=user["headers"],
    )
    assert project_response.status_code == status.HTTP_200_OK

    # Get project ID
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    projects = project_dao.filter(user_id=user["id"], name="Interfaces_Project")
    project = projects[0][0]
    project_id = project.id

    # Create an interface for this project
    interface_response = await client.post(
        "/v0/interfaces/",
        json={
            "project": "Interfaces_Project",
            "name": "TestInterface",
            "icon": "test-icon",
        },
        headers=user["headers"],
    )
    # Interface creation might return 200 or 201 depending on implementation
    assert interface_response.status_code in [
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    ]

    # Count interfaces before transfer
    interfaces_before = (
        dbsession.query(Interface).filter(Interface.project_id == project_id).all()
    )
    interface_count_before = len(interfaces_before)

    # Transfer to organization
    transfer_response = await client.post(
        f"/v0/project/{project_id}/transfer-to-organization",
        json={"organization_id": org_id},
        headers=user["headers"],
    )
    assert transfer_response.status_code == status.HTTP_200_OK

    # Verify interfaces still exist after transfer
    dbsession.expire_all()
    interfaces_after = (
        dbsession.query(Interface).filter(Interface.project_id == project_id).all()
    )

    assert len(interfaces_after) == interface_count_before
    if interface_count_before > 0:
        interface_names_before = {i.name for i in interfaces_before}
        interface_names_after = {i.name for i in interfaces_after}
        assert interface_names_before == interface_names_after


# ==================== Caching Performance Tests ====================


@pytest.mark.anyio
async def test_permission_cache_hits(dbsession):
    """Test that repeated permission checks use cache."""
    user_id = "cache_test_user"
    resource_type = "project"
    resource_id = 999
    permission = "project:read"

    # Clear cache to start fresh
    ResourceAccessDAO.clear_permission_cache()

    resource_access_dao = ResourceAccessDAO(dbsession)

    # First call - cache miss
    result1 = resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        permission,
    )

    # Verify result was cached
    cache_key = ResourceAccessDAO._get_cache_key(
        user_id,
        resource_type,
        resource_id,
        permission,
    )
    assert cache_key in ResourceAccessDAO._permission_cache

    # Second call - should be cache hit
    result2 = resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        permission,
    )

    # Results should be the same
    assert result1 == result2


@pytest.mark.anyio
async def test_cache_cleared_on_grant_access(dbsession):
    """Test that cache is cleared when granting access."""
    user_id = "grant_cache_user"
    resource_type = "project"
    resource_id = 888
    permission = "project:read"

    # Clear cache and populate it
    ResourceAccessDAO.clear_permission_cache()
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Make a permission check to populate cache
    resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        permission,
    )

    # Verify cache has entries
    assert len(ResourceAccessDAO._permission_cache) > 0

    # Get a role for granting
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Grant access (should clear cache)
    resource_access_dao.grant_access(
        resource_type=resource_type,
        resource_id=resource_id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=user_id,
    )

    # Verify cache was cleared
    assert len(ResourceAccessDAO._permission_cache) == 0


@pytest.mark.anyio
async def test_cache_cleared_on_revoke_access(dbsession):
    """Test that cache is cleared when revoking access."""
    user_id = "revoke_cache_user"
    resource_type = "project"
    resource_id = 777

    # Clear cache
    ResourceAccessDAO.clear_permission_cache()
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    # Grant access first
    resource_access_dao.grant_access(
        resource_type=resource_type,
        resource_id=resource_id,
        role_id=viewer_role.id,
        grantee_type="user",
        grantee_id=user_id,
    )
    dbsession.commit()

    # Make a permission check to populate cache
    resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        "project:read",
    )

    # Verify cache has entries
    assert len(ResourceAccessDAO._permission_cache) > 0

    # Revoke access (should clear cache)
    resource_access_dao.revoke_access(
        resource_type=resource_type,
        resource_id=resource_id,
        grantee_type="user",
        grantee_id=user_id,
    )

    # Verify cache was cleared
    assert len(ResourceAccessDAO._permission_cache) == 0


@pytest.mark.anyio
async def test_cache_size_limit(dbsession):
    """Test that cache doesn't grow unbounded."""
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Clear cache
    ResourceAccessDAO.clear_permission_cache()

    # Try to exceed cache limit
    cache_limit = ResourceAccessDAO._cache_size_limit

    # Fill cache beyond limit
    for i in range(cache_limit + 100):
        resource_access_dao.check_user_permission(
            f"user_{i}",
            "project",
            i,
            "project:read",
        )

        # Once cache hits limit, it should clear itself
        if len(ResourceAccessDAO._permission_cache) > cache_limit:
            # Cache should have been cleared
            assert len(ResourceAccessDAO._permission_cache) < cache_limit
            break
    else:
        # If we got through all iterations, cache should have been cleared at least once
        assert len(ResourceAccessDAO._permission_cache) <= cache_limit


@pytest.mark.anyio
async def test_cache_manual_clear(dbsession):
    """Test manual cache clearing."""
    resource_access_dao = ResourceAccessDAO(dbsession)

    # Clear cache
    ResourceAccessDAO.clear_permission_cache()
    assert len(ResourceAccessDAO._permission_cache) == 0

    # Populate cache
    for i in range(10):
        resource_access_dao.check_user_permission(
            f"user_{i}",
            "project",
            i,
            "project:read",
        )

    # Verify cache has entries
    assert len(ResourceAccessDAO._permission_cache) == 10

    # Manual clear
    ResourceAccessDAO.clear_permission_cache()

    # Verify cache is empty
    assert len(ResourceAccessDAO._permission_cache) == 0


@pytest.mark.anyio
async def test_different_cache_keys_for_different_permissions(dbsession):
    """Test that different permission checks use different cache keys."""
    resource_access_dao = ResourceAccessDAO(dbsession)
    ResourceAccessDAO.clear_permission_cache()

    user_id = "same_user"
    resource_id = 123

    # Check different permissions
    resource_access_dao.check_user_permission(
        user_id,
        "project",
        resource_id,
        "project:read",
    )
    resource_access_dao.check_user_permission(
        user_id,
        "project",
        resource_id,
        "project:write",
    )
    resource_access_dao.check_user_permission(
        user_id,
        "project",
        resource_id,
        "project:delete",
    )

    # Should have 3 different cache entries
    assert len(ResourceAccessDAO._permission_cache) == 3

    # Verify keys are different
    key1 = ResourceAccessDAO._get_cache_key(
        user_id,
        "project",
        resource_id,
        "project:read",
    )
    key2 = ResourceAccessDAO._get_cache_key(
        user_id,
        "project",
        resource_id,
        "project:write",
    )
    key3 = ResourceAccessDAO._get_cache_key(
        user_id,
        "project",
        resource_id,
        "project:delete",
    )

    assert key1 != key2 != key3
    assert all(k in ResourceAccessDAO._permission_cache for k in [key1, key2, key3])
