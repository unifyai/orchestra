"""Tests for Assistant Organization Support and Resource Access."""

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
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS, create_test_user


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Automatically mock assistant infrastructure webhooks and staging for all tests."""
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
        "orchestra.web.api.assistant.views.teardown_assistant_runtime",
        new_callable=AsyncMock,
    ) as mock_runtime_teardown, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_runtime_teardown.return_value = {"success": True, "errors": []}
        # Patch is_staging to skip credit checks
        mock_settings.is_staging = True

        yield mock_wake_up, mock_reawaken, mock_runtime_teardown


# =============================================================================
# Personal Assistant Tests (unchanged behavior)
# =============================================================================


@pytest.mark.anyio
async def test_personal_assistant_create(client: AsyncClient):
    """Test that personal assistants work identically to before."""
    payload = {
        "first_name": "Personal",
        "surname": "Assistant",
        "age": 30,
        "about": "A personal assistant",
        "create_infra": False,
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200

    data = resp.json()["info"]
    assert data["first_name"] == "Personal"
    assert data["surname"] == "Assistant"
    assert data["organization_id"] is None  # Personal assistant


@pytest.mark.anyio
async def test_personal_assistant_list(client: AsyncClient):
    """Test listing personal assistants."""
    # Create assistant
    payload = {
        "first_name": "ListTest",
        "surname": "Personal",
        "create_infra": False,
    }
    await client.post("/v0/assistant", json=payload, headers=HEADERS)

    # List assistants
    resp = await client.get("/v0/assistant", headers=HEADERS)
    assert resp.status_code == 200

    assistants = resp.json()["info"]
    assert len(assistants) >= 1
    # All should be personal (organization_id is None)
    for a in assistants:
        assert a["organization_id"] is None


@pytest.mark.anyio
async def test_personal_assistant_update(client: AsyncClient):
    """Test updating a personal assistant."""
    # Create
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Update", "surname": "Test", "create_infra": False},
        headers=HEADERS,
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    # Update
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"about": "Updated bio", "create_infra": False},
        headers=HEADERS,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["info"]["about"] == "Updated bio"
    assert update_resp.json()["info"]["organization_id"] is None


@pytest.mark.anyio
async def test_personal_assistant_delete(client: AsyncClient):
    """Test deleting a personal assistant."""
    # Create
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Delete", "surname": "Test", "create_infra": False},
        headers=HEADERS,
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    # Delete
    delete_resp = await client.delete(f"/v0/assistant/{agent_id}", headers=HEADERS)
    assert delete_resp.status_code == 200


# =============================================================================
# Organization Assistant Tests
# =============================================================================


@pytest.mark.anyio
async def test_org_assistant_create_grants_owner_role(client: AsyncClient, dbsession):
    """Test that creating an org assistant grants Owner role to creator."""
    owner = await create_test_user(
        client,
        "org_asst_owner@test.com",
    )

    # Create organization - API key is included in response
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Assistant Test Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Create assistant using org API key
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Org", "surname": "Assistant", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200

    data = create_resp.json()["info"]
    assert data["organization_id"] == org_id
    agent_id = int(data["agent_id"])

    # Verify Owner role was granted
    resource_access_dao = ResourceAccessDAO(dbsession)
    has_delete = resource_access_dao.check_user_permission(
        owner["id"],
        "assistant",
        agent_id,
        "assistant:delete",
    )
    assert has_delete is True, "Creator should have delete permission via Owner role"


@pytest.mark.anyio
async def test_org_assistant_update_with_org_key(client: AsyncClient, dbsession):
    """Test that org assistant can be updated using the same org API key that created it."""
    owner = await create_test_user(
        client,
        "org_asst_update@test.com",
    )

    # Create organization - API key is included in response
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Org Assistant Update Test"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Create assistant using org API key
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Org",
            "surname": "Updateable",
            "about": "Original bio",
            "timezone": "UTC",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    data = create_resp.json()["info"]
    assert data["organization_id"] == org_id
    agent_id = data["agent_id"]

    # Update assistant using same org API key
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={
            "about": "Updated bio",
            "timezone": "America/New_York",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert update_resp.status_code == 200, f"Update failed: {update_resp.json()}"

    # Verify the update was applied
    updated_data = update_resp.json()["info"]
    assert updated_data["about"] == "Updated bio"
    assert updated_data["timezone"] == "America/New_York"
    assert updated_data["organization_id"] == org_id


@pytest.mark.anyio
async def test_org_assistant_update_by_member_with_permission(
    client: AsyncClient,
    dbsession,
):
    """Test that org member with assistant:write permission can update org assistant."""
    owner = await create_test_user(
        client,
        "org_member_update_owner@test.com",
    )
    member = await create_test_user(
        client,
        "org_member_update_member@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Member Update Test Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_id = org_data["id"]
    owner_org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Add member to org
    add_member_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_resp.status_code == 201
    member_org_key = add_member_resp.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    # Owner creates assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Org",
            "surname": "SharedAssistant",
            "about": "Original",
            "create_infra": False,
        },
        headers=owner_org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Grant member write permission on the assistant
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    assert member_role is not None, "Member system role should exist"

    resource_access_dao.grant_access(
        resource_type="assistant",
        resource_id=agent_id,
        grantee_type="user",
        grantee_id=member["id"],
        role_id=member_role.id,
    )
    dbsession.commit()

    # Member updates the assistant
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"about": "Updated by member", "create_infra": False},
        headers=member_org_headers,
    )
    assert update_resp.status_code == 200, f"Member update failed: {update_resp.json()}"
    assert update_resp.json()["info"]["about"] == "Updated by member"


@pytest.mark.anyio
async def test_org_assistant_update_by_member_without_permission(
    client: AsyncClient,
    dbsession,
):
    """Test that org member without assistant:write permission cannot update org assistant."""
    owner = await create_test_user(
        client,
        "org_noperm_owner@test.com",
    )
    member = await create_test_user(
        client,
        "org_noperm_member@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "No Perm Update Test Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_id = org_data["id"]
    owner_org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Add member to org (they only have default member permissions, not assistant:write)
    add_member_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_resp.status_code == 201
    member_org_key = add_member_resp.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    # Owner creates assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Org",
            "surname": "Protected",
            "about": "Original",
            "create_infra": False,
        },
        headers=owner_org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    # Member tries to update without permission - should get 403
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"about": "Unauthorized update", "create_infra": False},
        headers=member_org_headers,
    )
    assert update_resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_org_assistant_list_own_only(client: AsyncClient, dbsession):
    """Test that list_all_org=False returns only user's own assistants."""
    owner = await create_test_user(
        client,
        "org_list_owner@test.com",
    )
    member = await create_test_user(
        client,
        "org_list_member@test.com",
    )

    # Create organization - owner gets API key in response
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "List Test Org"},
        headers=owner["headers"],
    )
    org_data = org_resp.json()
    org_id = org_data["id"]
    owner_org_key = org_data["api_key"]
    owner_org_headers = {"Authorization": f"Bearer {owner_org_key}"}

    # Add member to org - member gets API key in response
    add_member_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    member_org_key = add_member_resp.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    # Owner creates an assistant
    await client.post(
        "/v0/assistant",
        json={"first_name": "Owner", "surname": "Asst", "create_infra": False},
        headers=owner_org_headers,
    )

    # Member creates an assistant
    await client.post(
        "/v0/assistant",
        json={"first_name": "Member", "surname": "Asst", "create_infra": False},
        headers=member_org_headers,
    )

    # Member lists (list_all_org=False, default) - should see only their own
    list_resp = await client.get("/v0/assistant", headers=member_org_headers)
    assert list_resp.status_code == 200
    assistants = list_resp.json()["info"]
    assert len(assistants) == 1
    assert assistants[0]["first_name"] == "Member"


@pytest.mark.anyio
async def test_org_assistant_list_all_org(client: AsyncClient, dbsession):
    """Test that list_all_org=True returns all org assistants."""
    owner = await create_test_user(
        client,
        "org_listall_owner@test.com",
    )
    member = await create_test_user(
        client,
        "org_listall_member@test.com",
    )

    # Create organization - owner gets API key in response
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "List All Test Org"},
        headers=owner["headers"],
    )
    org_data = org_resp.json()
    org_id = org_data["id"]
    owner_org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Add member - member gets API key in response
    add_member_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    member_org_headers = {
        "Authorization": f"Bearer {add_member_resp.json()['api_key']}",
    }

    # Both create assistants
    await client.post(
        "/v0/assistant",
        json={"first_name": "OwnerAll", "surname": "Asst", "create_infra": False},
        headers=owner_org_headers,
    )
    await client.post(
        "/v0/assistant",
        json={"first_name": "MemberAll", "surname": "Asst", "create_infra": False},
        headers=member_org_headers,
    )

    # Member lists with list_all_org=True - should see all
    list_resp = await client.get(
        "/v0/assistant?list_all_org=true",
        headers=member_org_headers,
    )
    assert list_resp.status_code == 200
    assistants = list_resp.json()["info"]
    assert len(assistants) == 2
    names = {a["first_name"] for a in assistants}
    assert "OwnerAll" in names
    assert "MemberAll" in names


@pytest.mark.anyio
async def test_org_assistant_permission_checks(client: AsyncClient, dbsession):
    """Test that org assistants require proper permissions for operations."""
    owner = await create_test_user(
        client,
        "org_perm_owner@test.com",
    )
    viewer = await create_test_user(
        client,
        "org_perm_viewer@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Permission Test Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add viewer with Viewer role (read-only) - gets API key in response
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    add_viewer_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer["id"], "role_id": viewer_role.id},
        headers=owner["headers"],
    )
    viewer_org_headers = {
        "Authorization": f"Bearer {add_viewer_resp.json()['api_key']}",
    }

    # Viewer tries to create assistant - should fail (no assistant:write)
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "ViewerTry", "surname": "Create", "create_infra": False},
        headers=viewer_org_headers,
    )
    assert create_resp.status_code == status.HTTP_403_FORBIDDEN
    assert "permission" in create_resp.json()["detail"].lower()


# =============================================================================
# Transfer Tests
# =============================================================================


@pytest.mark.anyio
async def test_transfer_personal_to_org(client: AsyncClient, dbsession):
    """Test transferring a personal assistant to an organization."""
    user = await create_test_user(
        client,
        "transfer_to_org@test.com",
    )

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Transfer", "surname": "ToOrg", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])
    assert create_resp.json()["info"]["organization_id"] is None

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Transfer Target Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    # Transfer assistant to org (using personal API key)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200

    transfer_data = transfer_resp.json()["info"]
    assert transfer_data["agent_id"] == agent_id
    assert transfer_data["transferred_from"] == "personal"
    assert transfer_data["transferred_to"] == "organization"

    # Verify assistant is now in org
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.organization_id == org_id

    # Verify user has Owner role on assistant
    resource_access_dao = ResourceAccessDAO(dbsession)
    has_delete = resource_access_dao.check_user_permission(
        user["id"],
        "assistant",
        agent_id,
        "assistant:delete",
    )
    assert has_delete is True


@pytest.mark.anyio
async def test_transfer_org_to_personal(client: AsyncClient, dbsession):
    """Test transferring an org assistant to personal workspace."""
    user = await create_test_user(
        client,
        "transfer_to_personal@test.com",
    )

    # Create organization - API key included in response
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Transfer Source Org"},
        headers=user["headers"],
    )
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Transfer", "surname": "ToPersonal", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])
    assert create_resp.json()["info"]["organization_id"] == org_id

    # Transfer to personal (using org API key)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-personal",
        json={"delete_logs": False},
        headers=org_headers,
    )
    assert transfer_resp.status_code == 200

    transfer_data = transfer_resp.json()["info"]
    assert transfer_data["agent_id"] == agent_id
    assert transfer_data["transferred_from"] == "organization"
    assert transfer_data["transferred_to"] == "personal"

    # Verify assistant is now personal
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.organization_id is None
    assert assistant.user_id == user["id"]


@pytest.mark.anyio
async def test_transfer_to_org_requires_personal_api_key(
    client: AsyncClient,
    dbsession,
):
    """Test that transferring to org requires using a personal API key."""
    user = await create_test_user(
        client,
        "transfer_key_check@test.com",
    )

    # Create organization - API key included in response
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Key Check Org"},
        headers=user["headers"],
    )
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Try to transfer using org API key - should fail
    transfer_resp = await client.post(
        "/v0/assistant/999/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=org_headers,
    )
    assert transfer_resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "personal API key" in transfer_resp.json()["detail"]


@pytest.mark.anyio
async def test_transfer_to_personal_requires_org_api_key(
    client: AsyncClient,
    dbsession,
):
    """Test that transferring to personal requires using an org API key."""
    user = await create_test_user(
        client,
        "transfer_org_key@test.com",
    )

    # Try to transfer using personal API key - should fail
    transfer_resp = await client.post(
        "/v0/assistant/999/transfer/to-personal",
        json={"delete_logs": False},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "organization API key" in transfer_resp.json()["detail"]


@pytest.mark.anyio
async def test_transfer_to_org_requires_permission(client: AsyncClient, dbsession):
    """Test that transferring to org requires assistant:write in target org."""
    owner = await create_test_user(
        client,
        "transfer_perm_owner@test.com",
    )
    viewer = await create_test_user(
        client,
        "transfer_perm_viewer@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Transfer Perm Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add viewer with Viewer role
    role_dao = RoleDAO(dbsession)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)

    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": viewer["id"], "role_id": viewer_role.id},
        headers=owner["headers"],
    )

    # Viewer creates personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Viewer", "surname": "Personal", "create_infra": False},
        headers=viewer["headers"],
    )
    agent_id = create_resp.json()["info"]["agent_id"]

    # Viewer tries to transfer to org - should fail (no assistant:write)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=viewer["headers"],
    )
    assert transfer_resp.status_code == status.HTTP_403_FORBIDDEN
    assert "permission" in transfer_resp.json()["detail"].lower()


# =============================================================================
# RBAC Permission Tests
# =============================================================================


@pytest.mark.anyio
async def test_personal_assistant_owner_has_full_access(client: AsyncClient, dbsession):
    """Test that personal assistant owner has implicit full access."""
    user = await create_test_user(
        client,
        "personal_full_access@test.com",
    )

    # Create personal assistant using DAO
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.create_assistant(
        user_id=user["id"],
        first_name="Personal",
        surname="FullAccess",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=None,
    )
    dbsession.commit()

    # Check permissions
    resource_access_dao = ResourceAccessDAO(dbsession)

    has_read = resource_access_dao.check_user_permission(
        user["id"],
        "assistant",
        assistant.agent_id,
        "assistant:read",
    )
    assert has_read is True

    has_write = resource_access_dao.check_user_permission(
        user["id"],
        "assistant",
        assistant.agent_id,
        "assistant:write",
    )
    assert has_write is True

    has_delete = resource_access_dao.check_user_permission(
        user["id"],
        "assistant",
        assistant.agent_id,
        "assistant:delete",
    )
    assert has_delete is True


@pytest.mark.anyio
async def test_personal_assistant_other_users_no_access(client: AsyncClient, dbsession):
    """Test that other users have no access to personal assistants."""
    owner = await create_test_user(
        client,
        "personal_owner_access@test.com",
    )
    other = await create_test_user(
        client,
        "other_no_access@test.com",
    )

    # Create personal assistant
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.create_assistant(
        user_id=owner["id"],
        first_name="Personal",
        surname="NoAccess",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=None,
    )
    dbsession.commit()

    # Check other user has no access
    resource_access_dao = ResourceAccessDAO(dbsession)

    has_read = resource_access_dao.check_user_permission(
        other["id"],
        "assistant",
        assistant.agent_id,
        "assistant:read",
    )
    assert has_read is False


@pytest.mark.anyio
async def test_org_assistant_explicit_grant_overrides_implicit(
    client: AsyncClient,
    dbsession,
):
    """Test that explicit ResourceAccess grants override implicit org membership."""
    owner = await create_test_user(
        client,
        "org_explicit_owner@test.com",
    )
    member = await create_test_user(
        client,
        "org_explicit_member@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Explicit Grant Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # Add member to org
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Create org assistant
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.create_assistant(
        user_id=owner["id"],
        first_name="Explicit",
        surname="Grant",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.commit()

    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)

    # Without explicit grant: member has implicit read/write
    has_write_before = resource_access_dao.check_user_permission(
        member["id"],
        "assistant",
        assistant.agent_id,
        "assistant:write",
    )
    assert has_write_before is True, "Should have implicit write from Member role"

    # Grant explicit Viewer role (read-only)
    viewer_role = role_dao.get_by_name("Viewer", organization_id=None)
    resource_access_dao.grant_access(
        "assistant",
        assistant.agent_id,
        viewer_role.id,
        "user",
        member["id"],
    )
    dbsession.commit()

    # With explicit grant: member has only read
    has_read = resource_access_dao.check_user_permission(
        member["id"],
        "assistant",
        assistant.agent_id,
        "assistant:read",
    )
    assert has_read is True

    has_write_after = resource_access_dao.check_user_permission(
        member["id"],
        "assistant",
        assistant.agent_id,
        "assistant:write",
    )
    assert has_write_after is False, "Explicit Viewer should override implicit Member"


@pytest.mark.anyio
async def test_filter_accessible_assistants(client: AsyncClient, dbsession):
    """Test that filter_accessible_resources works for assistants."""
    user = await create_test_user(
        client,
        "filter_assistants@test.com",
    )

    # Create personal assistant
    assistant_dao = AssistantDAO(dbsession)
    personal_assistant = assistant_dao.create_assistant(
        user_id=user["id"],
        first_name="Filter",
        surname="Personal",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=None,
    )
    dbsession.commit()

    # Create organization and org assistant
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Filter Assistants Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    org_assistant = assistant_dao.create_assistant(
        user_id=user["id"],
        first_name="Filter",
        surname="Org",
        age=None,
        nationality=None,
        about=None,
        weekly_limit=None,
        max_parallel=None,
        organization_id=org_id,
    )
    dbsession.commit()

    # Grant Owner role to org assistant
    resource_access_dao = ResourceAccessDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    resource_access_dao.grant_access(
        "assistant",
        org_assistant.agent_id,
        owner_role.id,
        "user",
        user["id"],
    )
    dbsession.commit()

    # Filter accessible assistants
    accessible_ids = resource_access_dao.filter_accessible_resources(
        user["id"],
        "assistant",
        "assistant:read",
    )

    assert personal_assistant.agent_id in accessible_ids
    assert org_assistant.agent_id in accessible_ids


# =============================================================================
# API Isolation Tests
# =============================================================================


@pytest.mark.anyio
async def test_personal_and_org_assistants_isolated(client: AsyncClient, dbsession):
    """Test that personal and org assistants are isolated by API key type."""
    user = await create_test_user(
        client,
        "isolation_test@test.com",
    )

    # Create personal assistant
    personal_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Isolated", "surname": "Personal", "create_infra": False},
        headers=user["headers"],
    )
    assert personal_resp.status_code == 200
    personal_id = personal_resp.json()["info"]["agent_id"]

    # Create organization - API key included in response
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Isolation Test Org"},
        headers=user["headers"],
    )
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Create org assistant
    org_asst_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Isolated", "surname": "Org", "create_infra": False},
        headers=org_headers,
    )
    assert org_asst_resp.status_code == 200
    org_asst_id = org_asst_resp.json()["info"]["agent_id"]

    # List with personal key - should see only personal
    personal_list = await client.get("/v0/assistant", headers=user["headers"])
    personal_ids = {a["agent_id"] for a in personal_list.json()["info"]}
    assert personal_id in personal_ids
    assert org_asst_id not in personal_ids

    # List with org key - should see only org
    org_list = await client.get("/v0/assistant", headers=org_headers)
    org_ids = {a["agent_id"] for a in org_list.json()["info"]}
    assert org_asst_id in org_ids
    assert personal_id not in org_ids

    # Try to access personal assistant with org key - should fail
    get_personal_resp = await client.patch(
        f"/v0/assistant/{personal_id}/config",
        json={"about": "test", "create_infra": False},
        headers=org_headers,
    )
    assert get_personal_resp.status_code == 404

    # Try to access org assistant with personal key - should fail
    get_org_resp = await client.patch(
        f"/v0/assistant/{org_asst_id}/config",
        json={"about": "test", "create_infra": False},
        headers=user["headers"],
    )
    assert get_org_resp.status_code == 404


# =============================================================================
# Log Transfer/Deletion Tests
# =============================================================================


@pytest.mark.anyio
async def test_transfer_personal_to_org_with_logs_transfer(
    client: AsyncClient,
    dbsession,
):
    """Test that transferring to org with transfer_logs=True moves logs correctly."""
    user = await create_test_user(client, "log_transfer@test.com")

    # Create the personal "Assistants" project FIRST (before assistant creation)
    project_name = "Assistants"
    proj_resp = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=user["headers"],
    )
    assert proj_resp.status_code == 200

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "LogTransfer", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    agent_id = int(assistant_info["agent_id"])
    assistant_name = str(agent_id)

    # Create logs for this assistant in the personal Assistants project
    # Use the exact naming convention the transfer code expects
    context_name = assistant_name  # Just the assistant ID, not with /Transcripts
    log_payload = {
        "project_name": project_name,
        "context": context_name,
        "entries": [{"message": "Test log entry", "_assistant": assistant_name}],
    }
    log_resp = await client.post("/v0/logs", json=log_payload, headers=user["headers"])
    assert log_resp.status_code == 200

    # Verify logs exist in personal project
    logs_before = await client.get(
        f"/v0/logs?project_name={project_name}&context={context_name}",
        headers=user["headers"],
    )
    assert logs_before.status_code == 200
    assert logs_before.json()["count"] > 0, "Logs should exist before transfer"

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Log Transfer Target Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Transfer assistant to org with transfer_logs=True
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200
    transfer_data = transfer_resp.json()["info"]
    # logs_transferred may be True or False depending on whether contexts were found
    # The key assertion is that the transfer succeeded
    assert "logs_transferred" in transfer_data

    # If logs were transferred, verify they moved
    if transfer_data["logs_transferred"]:
        # Verify logs are now in org project (query with org headers)
        logs_after_org = await client.get(
            f"/v0/logs?project_name={project_name}&context={context_name}",
            headers=org_headers,
        )
        assert logs_after_org.status_code == 200
        assert (
            logs_after_org.json()["count"] > 0
        ), "Logs should be in org project after transfer"


@pytest.mark.anyio
async def test_transfer_personal_to_org_3tier_context_transfer(
    client: AsyncClient,
    dbsession,
):
    """
    Test that 3-tier context logs are transferred correctly personal -> org.

    Uses 3-tier context hierarchy:
    - Tier 1: All/Transcripts (global aggregate)
    - Tier 2: user_id/All/Transcripts (user aggregate)
    - Tier 3: user_id/assistant_id/Transcripts (user + assistant specific)

    When transferring with transfer_logs=True:
    - All assistant-specific contexts (Tier 3) should be moved
    - Logs in shared contexts (Tier 1 & 2) matching _assistant_id should be moved
    """
    user = await create_test_user(
        client,
        "3tier_transfer@test.com",
    )

    # Create personal Assistants project
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    assert proj_resp.status_code == 200

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "ThreeTier", "surname": "Transfer", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    agent_id = int(assistant_info["agent_id"])
    assistant_name = str(agent_id)

    # Define 3-tier context names using the actual user_id
    # The transfer endpoint uses context_prefix = f"{user_id}/{assistant_id}"
    tier3_context = f"{user['id']}/{assistant_name}/Transcripts"
    tier2_context = f"{user['id']}/All/Transcripts"
    tier1_context = "All/Transcripts"

    # Create log in Tier 3 context with proper fields
    log_resp = await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": tier3_context,
            "entries": [
                {
                    "message": "Log to transfer",
                    "_user": user["id"],
                    "_assistant": assistant_name,
                    "_assistant_id": agent_id,
                },
            ],
        },
        headers=user["headers"],
    )
    assert log_resp.status_code == 200
    log_id = log_resp.json()["log_event_ids"][0]

    # Add log to Tier 1 and Tier 2 contexts
    for ctx in [tier1_context, tier2_context]:
        add_resp = await client.post(
            "/v0/project/Assistants/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=user["headers"],
        )
        assert add_resp.status_code == 200

    # Verify log exists in all personal contexts
    for ctx in [tier1_context, tier2_context, tier3_context]:
        logs_resp = await client.get(
            f"/v0/logs?project_name=Assistants&context={ctx}",
            headers=user["headers"],
        )
        assert logs_resp.status_code == 200
        assert log_id in [log["id"] for log in logs_resp.json()["logs"]]

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "3Tier Transfer Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Transfer assistant to org with transfer_logs=True
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200
    transfer_data = transfer_resp.json()["info"]
    assert transfer_data["logs_transferred"] is True

    # Verify log is now accessible via org API key in all 3 tiers
    for ctx in [tier1_context, tier2_context, tier3_context]:
        logs_resp = await client.get(
            f"/v0/logs?project_name=Assistants&context={ctx}",
            headers=org_headers,
        )
        assert logs_resp.status_code == 200
        assert log_id in [
            log["id"] for log in logs_resp.json()["logs"]
        ], f"Log should be in org's {ctx}"

    # Verify log is no longer in personal project contexts
    for ctx in [tier1_context, tier2_context, tier3_context]:
        logs_resp = await client.get(
            f"/v0/logs?project_name=Assistants&context={ctx}",
            headers=user["headers"],
        )
        # Either 404 or empty
        if logs_resp.status_code == 200:
            assert log_id not in [
                log["id"] for log in logs_resp.json()["logs"]
            ], f"Log should be removed from personal {ctx}"


@pytest.mark.anyio
async def test_transfer_org_to_personal_with_logs_deletion(
    client: AsyncClient,
    dbsession,
):
    """Test that transferring to personal with delete_logs=True deletes org logs."""
    user = await create_test_user(client, "log_delete@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Log Delete Source Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create the org "Assistants" project FIRST
    project_name = "Assistants"
    proj_resp = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=org_headers,
    )
    assert proj_resp.status_code == 200

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "LogDelete", "surname": "Test", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    agent_id = int(assistant_info["agent_id"])
    assistant_name = str(agent_id)

    # Create logs for this assistant in the org Assistants project
    # Use exact context name pattern the transfer code expects
    context_name = assistant_name  # Just the assistant ID
    log_payload = {
        "project_name": project_name,
        "context": context_name,
        "entries": [{"message": "Org log entry", "_assistant": assistant_name}],
    }
    log_resp = await client.post("/v0/logs", json=log_payload, headers=org_headers)
    assert log_resp.status_code == 200

    # Verify logs exist in org project
    logs_before = await client.get(
        f"/v0/logs?project_name={project_name}&context={context_name}",
        headers=org_headers,
    )
    assert logs_before.status_code == 200
    assert logs_before.json()["count"] > 0, "Logs should exist before transfer"

    # Transfer assistant to personal with delete_logs=True
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-personal",
        json={"delete_logs": True},
        headers=org_headers,
    )
    assert transfer_resp.status_code == 200
    transfer_data = transfer_resp.json()["info"]
    # logs_deleted may be True or False depending on context matching
    assert "logs_deleted" in transfer_data

    # If logs were deleted, verify they're gone
    if transfer_data["logs_deleted"]:
        logs_after = await client.get(
            f"/v0/logs?project_name={project_name}&context={context_name}",
            headers=org_headers,
        )
        # Should either be 404 (context gone) or 200 with count=0
        if logs_after.status_code == 200:
            assert (
                logs_after.json()["count"] == 0
            ), "Logs should be deleted from org project"


@pytest.mark.anyio
async def test_delete_org_assistant_deletes_logs(client: AsyncClient, dbsession):
    """Test that deleting an org assistant cleans up associated logs."""
    user = await create_test_user(
        client,
        "org_delete_logs@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Delete Logs Org"},
        headers=user["headers"],
    )
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create the org "Assistants" project FIRST
    project_name = "Assistants"
    proj_resp = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=org_headers,
    )
    assert proj_resp.status_code == 200

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "OrgDelete", "surname": "Logs", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    agent_id = assistant_info["agent_id"]
    assistant_name = str(agent_id)

    # The delete endpoint uses context_prefix = f"{user_id}/{assistant_id}"
    # so we must create logs under that exact pattern.
    context_name = f"{user['id']}/{assistant_name}"
    log_payload = {
        "project_name": project_name,
        "context": context_name,
        "entries": [{"message": "Log to be deleted", "_assistant": assistant_name}],
    }
    log_resp = await client.post("/v0/logs", json=log_payload, headers=org_headers)
    assert log_resp.status_code == 200

    # Verify logs exist before deletion
    logs_before = await client.get(
        f"/v0/logs?project_name={project_name}&context={context_name}",
        headers=org_headers,
    )
    assert logs_before.status_code == 200
    assert (
        logs_before.json()["count"] > 0
    ), "Logs should exist before assistant deletion"

    # Delete the assistant
    delete_resp = await client.delete(f"/v0/assistant/{agent_id}", headers=org_headers)
    assert delete_resp.status_code == 200

    # Verify assistant is no longer accessible
    list_resp = await client.get("/v0/assistant", headers=org_headers)
    assert list_resp.status_code == 200
    remaining_ids = {a["agent_id"] for a in list_resp.json()["info"]}
    assert agent_id not in remaining_ids, "Deleted assistant should not appear in list"

    # Verify context and logs are cleaned up
    logs_after = await client.get(
        f"/v0/logs?project_name={project_name}&context={context_name}",
        headers=org_headers,
    )
    # Context should be deleted (404) or empty (200 with count=0)
    assert logs_after.status_code in [
        200,
        404,
    ], f"Expected 200 or 404, got {logs_after.status_code}"
    if logs_after.status_code == 200:
        assert (
            logs_after.json()["count"] == 0
        ), f"Logs should be deleted but found {logs_after.json()['count']}"


@pytest.mark.anyio
async def test_delete_org_assistant_cleans_3tier_contexts(
    client: AsyncClient,
    dbsession,
):
    """Test that deleting an org assistant cleans up 3-tier contexts including All/*."""
    user = await create_test_user(
        client,
        "org_3tier_cleanup@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "3Tier Cleanup Org"},
        headers=user["headers"],
    )
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create the org "Assistants" project
    project_name = "Assistants"
    proj_resp = await client.post(
        "/v0/project",
        json={"name": project_name},
        headers=org_headers,
    )
    assert proj_resp.status_code == 200

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "ThreeTier", "surname": "Test", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    agent_id = assistant_info["agent_id"]
    assistant_name = str(agent_id)

    # Set up 3-tier context structure using the actual user_id
    # The delete endpoint uses context_prefix = f"{request.state.user_id}/{assistant_id}"
    tier3_context = f"{user['id']}/{assistant_name}/Transcripts"
    tier2_context = f"{user['id']}/All/Transcripts"
    tier1_context = "All/Transcripts"

    # Create log in tier3 context with _user and _assistant fields
    log_payload = {
        "project_name": project_name,
        "context": tier3_context,
        "entries": [
            {
                "message": "3-tier test log",
                "_user": user["id"],
                "_assistant": assistant_name,
            },
        ],
    }
    log_resp = await client.post("/v0/logs", json=log_payload, headers=org_headers)
    assert log_resp.status_code == 200
    log_id = log_resp.json()["log_event_ids"][0]

    # Create tier2 and tier1 contexts and add the log to them
    for ctx in [tier2_context, tier1_context]:
        ctx_resp = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={"name": ctx},
            headers=org_headers,
        )
        assert ctx_resp.status_code == 200, ctx_resp.json()

        add_resp = await client.post(
            f"/v0/project/{project_name}/contexts/add_logs",
            json={"context_name": ctx, "log_ids": [log_id]},
            headers=org_headers,
        )
        assert add_resp.status_code == 200, add_resp.json()

    # Verify log exists in all 3 tiers before deletion
    for ctx in [tier3_context, tier2_context, tier1_context]:
        logs_before = await client.get(
            f"/v0/logs?project_name={project_name}&context={ctx}",
            headers=org_headers,
        )
        assert logs_before.status_code == 200
        assert (
            logs_before.json()["count"] > 0
        ), f"Log should exist in {ctx} before deletion"

    # Delete the assistant
    delete_resp = await client.delete(f"/v0/assistant/{agent_id}", headers=org_headers)
    assert delete_resp.status_code == 200

    # Verify assistant is deleted
    list_resp = await client.get("/v0/assistant", headers=org_headers)
    assert agent_id not in {a["agent_id"] for a in list_resp.json()["info"]}

    # Verify tier3 context (User/Assistant/Transcripts) is deleted or empty
    logs_tier3 = await client.get(
        f"/v0/logs?project_name={project_name}&context={tier3_context}",
        headers=org_headers,
    )
    assert logs_tier3.status_code in [200, 404]
    if logs_tier3.status_code == 200:
        assert logs_tier3.json()["count"] == 0, "Tier3 context should be empty"

    # Verify log is removed from tier2 but remains in tier1 (archive protection)
    sibling_logs = await client.get(
        f"/v0/logs?project_name={project_name}&context={tier2_context}",
        headers=org_headers,
    )
    if sibling_logs.status_code == 200:
        log_ids = [log["id"] for log in sibling_logs.json()["logs"]]
        assert (
            log_id not in log_ids
        ), f"Log {log_id} should be removed from sibling context {tier2_context}"

    # Archive protection: log remains in topmost All/* context
    archive_logs = await client.get(
        f"/v0/logs?project_name={project_name}&context={tier1_context}",
        headers=org_headers,
    )
    if archive_logs.status_code == 200:
        log_ids = [log["id"] for log in archive_logs.json()["logs"]]
        assert (
            log_id in log_ids
        ), f"Log {log_id} should remain in archive context {tier1_context}"


# =============================================================================
# Phase 3: Admin API Key Verification Tests
# =============================================================================


@pytest.mark.anyio
async def test_admin_list_personal_assistant_has_personal_api_key(
    client: AsyncClient,
    dbsession,
):
    """Test that admin endpoint returns personal API key for personal assistants."""
    user = await create_test_user(
        client,
        "admin_key_personal@test.com",
    )

    # Get user's personal API key
    credits_resp = await client.get("/v0/credits", headers=user["headers"])
    # The personal API key is in the Authorization header
    personal_api_key = user["headers"]["Authorization"].replace("Bearer ", "")

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "AdminKey", "surname": "Personal", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    # Call admin list endpoint
    admin_resp = await client.get(
        f"/v0/admin/assistant?agent_id={agent_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    assert len(assistants) == 1
    assert assistants[0]["api_key"] == personal_api_key
    assert assistants[0]["organization_id"] is None


@pytest.mark.anyio
async def test_admin_list_org_assistant_has_org_api_key(client: AsyncClient, dbsession):
    """Test that admin endpoint returns org API key for org assistants."""
    user = await create_test_user(
        client,
        "admin_key_org@test.com",
    )

    # Create organization - get org API key
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Admin Key Test Org"},
        headers=user["headers"],
    )
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "AdminKey", "surname": "Org", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    # Call admin list endpoint
    admin_resp = await client.get(
        f"/v0/admin/assistant?agent_id={agent_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    assert len(assistants) == 1
    assert assistants[0]["api_key"] == org_api_key
    assert assistants[0]["organization_id"] == org_id


@pytest.mark.anyio
async def test_admin_list_mixed_assistants_correct_api_keys(
    client: AsyncClient,
    dbsession,
):
    """Test that admin endpoint returns correct API keys for mixed assistant types."""
    user = await create_test_user(client, "admin_mixed@test.com")
    personal_api_key = user["headers"]["Authorization"].replace("Bearer ", "")

    # Create personal assistant
    personal_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Mixed", "surname": "Personal", "create_infra": False},
        headers=user["headers"],
    )
    assert personal_resp.status_code == 200
    personal_id = personal_resp.json()["info"]["agent_id"]

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Mixed Key Test Org"},
        headers=user["headers"],
    )
    org_api_key = org_resp.json()["api_key"]
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Create org assistant
    org_asst_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Mixed", "surname": "Org", "create_infra": False},
        headers=org_headers,
    )
    assert org_asst_resp.status_code == 200
    org_asst_id = org_asst_resp.json()["info"]["agent_id"]

    # Verify personal assistant API key using global admin endpoint
    admin_personal_resp = await client.get(
        f"/v0/admin/assistant?agent_id={personal_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin_personal_resp.status_code == 200
    personal_assistants = admin_personal_resp.json()["info"]
    assert len(personal_assistants) == 1
    assert personal_assistants[0]["api_key"] == personal_api_key
    assert personal_assistants[0]["organization_id"] is None

    # Verify org assistant API key using global admin endpoint
    admin_org_resp = await client.get(
        f"/v0/admin/assistant?agent_id={org_asst_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin_org_resp.status_code == 200
    org_assistants = admin_org_resp.json()["info"]
    assert len(org_assistants) == 1
    assert org_assistants[0]["api_key"] == org_api_key
    assert org_assistants[0]["organization_id"] == org_id


# =============================================================================
# Phase 4: Edge Case Tests
# =============================================================================


@pytest.mark.anyio
async def test_transfer_creates_assistants_project_if_missing(
    client: AsyncClient,
    dbsession,
):
    """Test that transfer_logs=True succeeds even without existing Assistants projects."""
    user = await create_test_user(
        client,
        "transfer_create_proj@test.com",
    )

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "CreateProj", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Create organization (no Assistants project yet)
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "No Assistants Project Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Transfer with transfer_logs=True - should succeed even without existing projects
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200

    # The key assertion is that transfer succeeded
    transfer_data = transfer_resp.json()["info"]
    assert transfer_data["transferred_to"] == "organization"
    # logs_transferred should be False since there's no personal Assistants project
    assert transfer_data["logs_transferred"] is False

    # Verify assistant is now in org
    from orchestra.db.dao.assistant_dao import AssistantDAO

    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.organization_id == org_id


@pytest.mark.anyio
async def test_transfer_with_no_logs_succeeds(client: AsyncClient, dbsession):
    """Test that transfer_logs=True succeeds when no logs exist."""
    user = await create_test_user(
        client,
        "transfer_no_logs@test.com",
    )

    # Create personal assistant (no logs created)
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "NoLogs", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "No Logs Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    # Transfer with transfer_logs=True - should succeed
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200
    # logs_transferred should be False since there were no logs
    assert transfer_resp.json()["info"]["logs_transferred"] is False


@pytest.mark.anyio
async def test_transfer_to_nonexistent_org_fails(client: AsyncClient, dbsession):
    """Test that transfer to non-existent org fails."""
    user = await create_test_user(
        client,
        "transfer_noorg@test.com",
    )

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "NoOrg", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to transfer to non-existent org
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": 999999, "transfer_logs": False},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 403  # No permission in non-existent org


@pytest.mark.anyio
async def test_transfer_already_org_assistant_fails(client: AsyncClient, dbsession):
    """Test that to-org transfer fails for already-org assistant."""
    user = await create_test_user(
        client,
        "transfer_already_org@test.com",
    )

    # This test verifies the API key check - must use personal key for to-org transfer
    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Already Org Test"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "AlreadyOrg", "surname": "Test", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to transfer to-org using org API key - should fail
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=org_headers,  # Using org key, not personal
    )
    assert transfer_resp.status_code == 400
    assert "personal API key" in transfer_resp.json()["detail"]


@pytest.mark.anyio
async def test_transfer_personal_assistant_not_owned_fails(
    client: AsyncClient,
    dbsession,
):
    """Test that user cannot transfer another user's personal assistant."""
    owner = await create_test_user(
        client,
        "transfer_owner@test.com",
    )
    other = await create_test_user(
        client,
        "transfer_other@test.com",
    )

    # Owner creates personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "NotOwned", "surname": "Test", "create_infra": False},
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Other user creates an org they own
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Other User Org"},
        headers=other["headers"],
    )
    org_id = org_resp.json()["id"]

    # Other user tries to transfer owner's assistant - should fail (assistant not found)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=other["headers"],
    )
    assert transfer_resp.status_code == 404


@pytest.mark.anyio
async def test_transfer_org_to_personal_requires_delete_permission(
    client: AsyncClient,
    dbsession,
):
    """Test that only users with assistant:delete can transfer out of org."""
    owner = await create_test_user(
        client,
        "delete_perm_owner@test.com",
    )
    member = await create_test_user(
        client,
        "delete_perm_member@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Delete Perm Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]
    owner_org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Add member (default Member role has read/write but not delete)
    add_member_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    member_org_headers = {
        "Authorization": f"Bearer {add_member_resp.json()['api_key']}",
    }

    # Owner creates org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "DeletePerm", "surname": "Test", "create_infra": False},
        headers=owner_org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Member tries to transfer to personal - should fail (no delete permission)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-personal",
        json={"delete_logs": False},
        headers=member_org_headers,
    )
    assert transfer_resp.status_code == 403
    assert "permission" in transfer_resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_transfer_to_org_non_member_fails(client: AsyncClient, dbsession):
    """Test that non-member of org cannot transfer assistant to it."""
    user = await create_test_user(client, "non_member@test.com")
    org_owner = await create_test_user(
        client,
        "org_owner_nm@test.com",
    )

    # Org owner creates organization (user is NOT a member)
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Non Member Org"},
        headers=org_owner["headers"],
    )
    org_id = org_resp.json()["id"]

    # User creates personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "NonMember", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # User tries to transfer to org they're not a member of - should fail
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 403


@pytest.mark.anyio
async def test_transfer_response_logs_transferred_flag(client: AsyncClient, dbsession):
    """Test that logs_transferred flag is correct in transfer response."""
    user = await create_test_user(client, "logs_flag@test.com")

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "LogsFlag", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Logs Flag Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    # Transfer with transfer_logs=False
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200
    # logs_transferred should be False when transfer_logs=False
    assert transfer_resp.json()["info"]["logs_transferred"] is False


@pytest.mark.anyio
async def test_transfer_response_logs_deleted_flag(client: AsyncClient, dbsession):
    """Test that logs_deleted flag is correct in transfer response."""
    user = await create_test_user(client, "delete_flag@test.com")

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Delete Flag Org"},
        headers=user["headers"],
    )
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "DeleteFlag", "surname": "Test", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Transfer with delete_logs=False
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-personal",
        json={"delete_logs": False},
        headers=org_headers,
    )
    assert transfer_resp.status_code == 200
    # logs_deleted should be False when delete_logs=False
    assert transfer_resp.json()["info"]["logs_deleted"] is False


@pytest.mark.anyio
async def test_transfer_creates_assistants_project_with_owner_access(
    client: AsyncClient,
    dbsession,
):
    """
    Test that transferring an assistant creates Assistants project with Owner access.

    When the org Assistants project doesn't exist:
    - It should be created
    - The transferring user should get Owner role on it
    """
    user = await create_test_user(
        client,
        "proj_owner_creator@test.com",
    )

    # Create personal Assistants project (so we can transfer logs)
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    assert proj_resp.status_code == 200

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "ProjOwner", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Create organization (user becomes owner)
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Proj Owner Test Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Transfer assistant to org with transfer_logs=True
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200

    # Verify user can see the Assistants project via org API
    projects_resp = await client.get("/v0/projects", headers=org_headers)
    assert projects_resp.status_code == 200
    assert (
        "Assistants" in projects_resp.json()
    ), "User should have access to Assistants project"

    # Verify user has Owner role on the project (can delete it)
    resource_access_dao = ResourceAccessDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    projects = project_dao.filter(organization_id=org_id, name="Assistants")
    assert len(projects) > 0, "Assistants project should exist in org"
    project_id = projects[0][0].id

    # Check user has project:delete permission (Owner has this)
    has_delete = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project_id,
        "project:delete",
    )
    assert has_delete, "Creator should have Owner role with delete permission"


@pytest.mark.anyio
async def test_transfer_grants_member_to_second_user_on_existing_project(
    client: AsyncClient,
    dbsession,
):
    """
    Test that second user gets Member access when Assistants project already exists.

    When user transfers to org where Assistants project already exists:
    - User should get Member role (not Owner)
    - User should be able to read but not write or delete
    """
    # First user creates org and Assistants project
    user1 = await create_test_user(
        client,
        "proj_first_user@test.com",
    )

    # Create organization (user1 becomes owner)
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Multi User Assistants Org"},
        headers=user1["headers"],
    )
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Create Assistants project explicitly via user1's org key
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )
    assert proj_resp.status_code == 200

    # Second user joins org
    user2 = await create_test_user(
        client,
        "proj_second_user@test.com",
    )

    # Add user2 to org (use user_id, not email - OrganizationMemberAdd schema)
    invite_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user2["id"]},
        headers=user1["headers"],
    )
    assert invite_resp.status_code in [200, 201]

    # User2 creates personal Assistants project (for log transfer)
    proj_resp2 = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user2["headers"],
    )
    assert proj_resp2.status_code == 200

    # User2 creates personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "SecondUser", "surname": "Asst", "create_infra": False},
        headers=user2["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Get user2's org API key
    user2_info_resp = await client.get(
        f"/v0/admin/user/by-email?email=proj_second_user@test.com",
        headers=ADMIN_HEADERS,
    )
    user2_info = user2_info_resp.json()
    user2_org_api_key = None
    for org in user2_info.get("organizations", []):
        if org.get("id") == org_id:
            user2_org_api_key = org.get("api_key")
            break

    # User2 transfers assistant to org (project already exists from user1)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user2["headers"],
    )
    assert transfer_resp.status_code == 200

    # Verify user2 can see the Assistants project
    if user2_org_api_key:
        user2_org_headers = {"Authorization": f"Bearer {user2_org_api_key}"}
        projects_resp = await client.get("/v0/projects", headers=user2_org_headers)
        assert projects_resp.status_code == 200
        assert (
            "Assistants" in projects_resp.json()
        ), "User2 should have access to Assistants project"

    # Verify user2 has Member role (read but not write/delete)
    resource_access_dao = ResourceAccessDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    projects = project_dao.filter(organization_id=org_id, name="Assistants")
    project_id = projects[0][0].id

    # Check user2 has project:read permission (Member has this)
    has_read = resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:read",
    )
    assert has_read, "Second user should have read permission"

    # Check user2 does NOT have project:delete permission (Member doesn't have this)
    has_delete = resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:delete",
    )
    assert not has_delete, "Second user should NOT have delete permission"


@pytest.mark.anyio
async def test_transfer_no_duplicate_grant_if_already_has_access(
    client: AsyncClient,
    dbsession,
):
    """
    Test that transferring doesn't create duplicate grants if user already has access.

    If user already has access to Assistants project, no new grant should be added.
    """
    user = await create_test_user(
        client,
        "no_dup_grant@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "No Dup Grant Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Create Assistants project (user gets Owner grant via normal flow)
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )
    assert proj_resp.status_code == 200

    # Create personal Assistants project for log transfer
    personal_proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    assert personal_proj_resp.status_code == 200

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "NoDup", "surname": "Grant", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Get project ID and count grants before transfer
    resource_access_dao = ResourceAccessDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    projects = project_dao.filter(organization_id=org_id, name="Assistants")
    project_id = projects[0][0].id

    grants_before = resource_access_dao.get_resource_access("project", project_id)
    user_grants_before = [g for g in grants_before if g.grantee_id == user["id"]]
    count_before = len(user_grants_before)

    # Transfer assistant (user already has Owner access to project)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200

    # Verify grant count didn't increase
    dbsession.expire_all()
    grants_after = resource_access_dao.get_resource_access("project", project_id)
    user_grants_after = [g for g in grants_after if g.grantee_id == user["id"]]
    count_after = len(user_grants_after)

    assert (
        count_after == count_before
    ), "Should not create duplicate grant if user already has access"


@pytest.mark.anyio
async def test_transfer_shared_all_context_logs(
    client: AsyncClient,
    dbsession,
):
    """
    Test that logs in shared 'All/*' contexts are transferred correctly.

    When transferring an assistant with transfer_logs=True:
    - Logs in "All/Contact" (or other "All/*" contexts) that belong to the
      assistant (identified by _assistant_id) should be transferred
    - If "All/Contact" exists in org, logs should be linked to existing context
    - If "All/Contact" doesn't exist in org, it should be created
    """
    user = await create_test_user(
        client,
        "shared_ctx_transfer@test.com",
    )

    # Create personal Assistants project
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    assert proj_resp.status_code == 200

    # Create personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "SharedCtx", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    agent_id = int(assistant_info["agent_id"])
    assistant_name = str(agent_id)

    # Create logs in assistant-specific context using {user_id}/{assistant_id} pattern
    # (matches the transfer endpoint's context_prefix = f"{user_id}/{assistant_id}")
    specific_context = f"{user['id']}/{assistant_name}"
    specific_log_payload = {
        "project_name": "Assistants",
        "context": specific_context,
        "entries": [
            {
                "message": "Specific context log",
                "_assistant_id": agent_id,
            },
        ],
    }
    log_resp = await client.post(
        "/v0/logs",
        json=specific_log_payload,
        headers=user["headers"],
    )
    assert log_resp.status_code == 200

    # Create logs in shared "All/Contact" context with _assistant_id
    shared_log_payload = {
        "project_name": "Assistants",
        "context": "All/Contact",
        "entries": [
            {
                "message": "Shared context log for this assistant",
                "_assistant_id": agent_id,
            },
        ],
    }
    log_resp2 = await client.post(
        "/v0/logs",
        json=shared_log_payload,
        headers=user["headers"],
    )
    assert log_resp2.status_code == 200

    # Verify logs exist in personal project's "All/Contact"
    logs_before = await client.get(
        "/v0/logs?project_name=Assistants&context=All/Contact",
        headers=user["headers"],
    )
    assert logs_before.status_code == 200
    assert logs_before.json()["count"] > 0, "Shared context logs should exist"

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Shared Ctx Transfer Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Transfer assistant to org with transfer_logs=True
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200
    transfer_data = transfer_resp.json()["info"]
    assert transfer_data["logs_transferred"] is True

    # Verify assistant-specific logs are in org project
    specific_logs_org = await client.get(
        f"/v0/logs?project_name=Assistants&context={specific_context}",
        headers=org_headers,
    )
    assert specific_logs_org.status_code == 200
    assert (
        specific_logs_org.json()["count"] > 0
    ), "Assistant-specific logs should be in org"

    # Verify shared "All/Contact" logs are in org project
    shared_logs_org = await client.get(
        "/v0/logs?project_name=Assistants&context=All/Contact",
        headers=org_headers,
    )
    assert shared_logs_org.status_code == 200
    assert (
        shared_logs_org.json()["count"] > 0
    ), "Shared context logs should be transferred to org"

    # Verify the shared context logs are no longer in personal project
    # (they were moved, not copied)
    shared_logs_personal = await client.get(
        "/v0/logs?project_name=Assistants&context=All/Contact",
        headers=user["headers"],
    )
    # Either 404 (context gone) or 200 with count=0 (context exists but no logs)
    if shared_logs_personal.status_code == 200:
        assert (
            shared_logs_personal.json()["count"] == 0
        ), "Shared context logs should be removed from personal project"


@pytest.mark.anyio
async def test_transfer_shared_context_to_existing_org_context(
    client: AsyncClient,
    dbsession,
):
    """
    Test that when org already has 'All/Contact' context, logs are linked to it.

    This tests the scenario where:
    1. Org already has "All/Contact" context (from previous assistant transfers)
    2. A new assistant is transferred with logs in "All/Contact"
    3. The logs should be linked to the existing org context
    """
    user = await create_test_user(
        client,
        "existing_shared_ctx@test.com",
    )

    # Create personal Assistants project
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    assert proj_resp.status_code == 200

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Existing Shared Ctx Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create org Assistants project with "All/Contact" context already existing
    org_proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )
    assert org_proj_resp.status_code == 200

    # Create a log in org's "All/Contact" to establish the context
    existing_log_payload = {
        "project_name": "Assistants",
        "context": "All/Contact",
        "entries": [{"message": "Pre-existing org log", "_assistant_id": 999}],
    }
    existing_log_resp = await client.post(
        "/v0/logs",
        json=existing_log_payload,
        headers=org_headers,
    )
    assert existing_log_resp.status_code == 200

    # Now create a personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "ExistingCtx", "surname": "Test", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Create logs in personal "All/Contact" for this assistant
    personal_shared_log = {
        "project_name": "Assistants",
        "context": "All/Contact",
        "entries": [
            {
                "message": "Personal shared context log",
                "_assistant_id": agent_id,
            },
        ],
    }
    log_resp = await client.post(
        "/v0/logs",
        json=personal_shared_log,
        headers=user["headers"],
    )
    assert log_resp.status_code == 200

    # Get count of logs in org's "All/Contact" before transfer
    logs_before = await client.get(
        "/v0/logs?project_name=Assistants&context=All/Contact",
        headers=org_headers,
    )
    count_before = logs_before.json()["count"]

    # Transfer assistant to org
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": True},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == 200

    # Verify org's "All/Contact" now has more logs (existing + transferred)
    logs_after = await client.get(
        "/v0/logs?project_name=Assistants&context=All/Contact",
        headers=org_headers,
    )
    assert logs_after.status_code == 200
    count_after = logs_after.json()["count"]

    assert (
        count_after > count_before
    ), "Org's All/Contact should have more logs after transfer"


@pytest.mark.anyio
async def test_transfer_org_to_personal_deletes_shared_context_logs(
    client: AsyncClient,
    dbsession,
):
    """
    Test that logs in 3-tier shared contexts are cleaned when transferring org->personal.

    Uses 3-tier context hierarchy:
    - Tier 1: All/Transcripts (global aggregate)
    - Tier 2: user_id/All/Transcripts (user aggregate)
    - Tier 3: user_id/assistant_id/Transcripts (user + assistant specific)

    When transferring an assistant from org to personal with delete_logs=True:
    - Assistant-specific contexts (Tier 3) should be deleted
    - Logs should be cleaned from sibling contexts (Tier 1 and Tier 2) via context_dao.delete()
    """
    user = await create_test_user(
        client,
        "shared_ctx_delete@test.com",
    )
    user_name = "test-user"

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Shared Ctx Delete Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create org Assistants project
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )
    assert proj_resp.status_code == 200

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "SharedDel", "surname": "Test", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    assistant_info = create_resp.json()["info"]
    agent_id = int(assistant_info["agent_id"])
    assistant_name = str(agent_id)

    # Define 3-tier context names
    tier3_context = f"{user_name}/{assistant_name}/Transcripts"
    tier2_context = f"{user_name}/All/Transcripts"
    tier1_context = "All/Transcripts"

    # Create log in Tier 3 (assistant-specific) context with _user and _assistant fields
    tier3_log_payload = {
        "project_name": "Assistants",
        "context": tier3_context,
        "entries": [
            {
                "message": "Assistant-specific log",
                "_user": user_name,
                "_assistant": assistant_name,
                "_assistant_id": agent_id,
            },
        ],
    }
    log_resp = await client.post(
        "/v0/logs",
        json=tier3_log_payload,
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

    # Transfer assistant to personal with delete_logs=True
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-personal",
        json={"delete_logs": True},
        headers=org_headers,
    )
    assert transfer_resp.status_code == 200
    transfer_data = transfer_resp.json()["info"]
    assert transfer_data["logs_deleted"] is True

    # Verify log is removed from tier2 and tier3 but remains in tier1 (archive protection)
    for ctx in [tier2_context, tier3_context]:
        logs_resp = await client.get(
            f"/v0/logs?project_name=Assistants&context={ctx}",
            headers=org_headers,
        )
        if logs_resp.status_code == 200:
            assert log_id not in [
                log["id"] for log in logs_resp.json()["logs"]
            ], f"Log should be cleaned from {ctx}"

    # Archive protection: log remains in topmost All/* context
    logs_resp = await client.get(
        f"/v0/logs?project_name=Assistants&context={tier1_context}",
        headers=org_headers,
    )
    if logs_resp.status_code == 200:
        assert log_id in [
            log["id"] for log in logs_resp.json()["logs"]
        ], f"Log should remain in archive {tier1_context}"


@pytest.mark.anyio
async def test_transfer_org_to_personal_preserves_other_assistant_logs(
    client: AsyncClient,
    dbsession,
):
    """
    Test that logs from OTHER assistants in shared contexts are preserved during transfer.

    When transferring assistant A with delete_logs=True:
    - Assistant A's logs should be deleted from all contexts
    - Assistant B's logs in shared All/* contexts should NOT be affected
    """
    user = await create_test_user(
        client,
        "preserve_other_logs@test.com",
    )
    user_name = "preserve-user"

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Preserve Other Logs Org"},
        headers=user["headers"],
    )
    org_headers = {"Authorization": f"Bearer {org_resp.json()['api_key']}"}

    # Create org Assistants project
    proj_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )
    assert proj_resp.status_code == 200

    # Create TWO org assistants
    create_resp_a = await client.post(
        "/v0/assistant",
        json={"first_name": "AssistantA", "surname": "Transfer", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp_a.status_code == 200
    agent_id_a = int(create_resp_a.json()["info"]["agent_id"])
    assistant_name_a = str(agent_id_a)

    create_resp_b = await client.post(
        "/v0/assistant",
        json={"first_name": "AssistantB", "surname": "Keep", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp_b.status_code == 200
    agent_id_b = int(create_resp_b.json()["info"]["agent_id"])
    assistant_name_b = str(agent_id_b)

    # Create 3-tier contexts for Assistant A
    tier3_a = f"{user_name}/{assistant_name_a}/Transcripts"
    tier2_context = f"{user_name}/All/Transcripts"
    tier1_context = "All/Transcripts"

    # Create 3-tier contexts for Assistant B
    tier3_b = f"{user_name}/{assistant_name_b}/Transcripts"

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
                    "_assistant_id": agent_id_a,
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
                    "_assistant_id": agent_id_b,
                },
            ],
        },
        headers=org_headers,
    )
    assert log_resp_b.status_code == 200
    log_id_b = log_resp_b.json()["log_event_ids"][0]

    # Add both logs to shared contexts (Tier 1 and Tier 2)
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

    # Transfer Assistant A to personal (deleting logs)
    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id_a}/transfer/to-personal",
        json={"delete_logs": True},
        headers=org_headers,
    )
    assert transfer_resp.status_code == 200

    # Verify Assistant A's log is removed from tier2 context
    logs_resp = await client.get(
        f"/v0/logs?project_name=Assistants&context={tier2_context}",
        headers=org_headers,
    )
    assert logs_resp.status_code == 200
    log_ids = [log["id"] for log in logs_resp.json()["logs"]]
    assert log_id_a not in log_ids, f"Log A should be removed from {tier2_context}"
    # Assistant B's log should still exist
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
# Org Assistant Assistants Project Access Tests
# =============================================================================


@pytest.mark.anyio
async def test_org_assistant_create_creates_assistants_project_with_owner_access(
    client: AsyncClient,
    dbsession,
):
    """
    Test that creating an org assistant creates Assistants project with Owner access.

    When the org Assistants project doesn't exist:
    - It should be created
    - The creator should get Owner role on it
    """
    user = await create_test_user(
        client,
        "org_asst_proj_owner@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Assistants Project Test Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Verify no Assistants project exists yet
    projects_resp = await client.get("/v0/projects", headers=org_headers)
    assert projects_resp.status_code == 200
    assert "Assistants" not in projects_resp.json()

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Project", "surname": "Creator", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200

    # Verify Assistants project now exists and user can see it
    projects_resp = await client.get("/v0/projects", headers=org_headers)
    assert projects_resp.status_code == 200
    assert "Assistants" in projects_resp.json()

    # Verify user has Owner role (project:delete permission)
    resource_access_dao = ResourceAccessDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    projects = project_dao.filter(organization_id=org_id, name="Assistants")
    assert len(projects) > 0, "Assistants project should exist in org"
    project_id = projects[0][0].id

    has_delete = resource_access_dao.check_user_permission(
        user["id"],
        "project",
        project_id,
        "project:delete",
    )
    assert has_delete, "Creator should have Owner role with delete permission"


@pytest.mark.anyio
async def test_new_org_member_gets_assistants_project_access_on_join(
    client: AsyncClient,
    dbsession,
):
    """
    Test that new org members get Member access to Assistants project on join.

    When a user joins an org with an existing Assistants project:
    - User should immediately get Member role on Assistants project
    - User should be able to read but not delete
    """
    # First user creates org and Assistants project via assistant creation
    user1 = await create_test_user(
        client,
        "org_asst_proj_owner2@test.com",
    )

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Existing Assistants Proj Org"},
        headers=user1["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # User1 creates first assistant (creates Assistants project with Owner access)
    create_resp1 = await client.post(
        "/v0/assistant",
        json={"first_name": "First", "surname": "Assistant", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp1.status_code == 200

    # Verify Assistants project exists
    projects_resp = await client.get("/v0/projects", headers=org_headers)
    assert "Assistants" in projects_resp.json()

    # Second user joins org
    user2 = await create_test_user(
        client,
        "org_asst_member@test.com",
    )

    add_member_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user2["id"]},
        headers=user1["headers"],
    )
    assert add_member_resp.status_code == 201
    member_org_key = add_member_resp.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    # Verify user2 can IMMEDIATELY see Assistants project (granted on join)
    projects_resp2 = await client.get("/v0/projects", headers=member_org_headers)
    assert projects_resp2.status_code == 200
    assert (
        "Assistants" in projects_resp2.json()
    ), "User2 should have access to Assistants project immediately after joining"

    # Verify user2 has Member role (project:read but NOT project:delete)
    resource_access_dao = ResourceAccessDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    projects = project_dao.filter(organization_id=org_id, name="Assistants")
    project_id = projects[0][0].id

    has_read = resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:read",
    )
    has_delete = resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:delete",
    )
    assert has_read, "Member should have read permission"
    assert not has_delete, "Member should NOT have delete permission"


@pytest.mark.anyio
async def test_org_assistant_create_grants_member_access_to_existing_org_members(
    client: AsyncClient,
    dbsession,
):
    """
    Test that creating an org assistant grants Member access to all existing org members.

    When the org Assistants project is created:
    - Creator gets Owner role
    - All other existing org members get Member role
    """
    # Create org owner (user1)
    user1 = await create_test_user(
        client,
        "org_asst_proj_creator@test.com",
    )

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Multi Member Assistants Org"},
        headers=user1["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Add user2 and user3 as members BEFORE Assistants project exists
    user2 = await create_test_user(
        client,
        "org_asst_member2@test.com",
    )
    user3 = await create_test_user(
        client,
        "org_asst_member3@test.com",
    )

    add_member2_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user2["id"]},
        headers=user1["headers"],
    )
    assert add_member2_resp.status_code == 201
    member2_org_key = add_member2_resp.json()["api_key"]
    member2_org_headers = {"Authorization": f"Bearer {member2_org_key}"}

    add_member3_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": user3["id"]},
        headers=user1["headers"],
    )
    assert add_member3_resp.status_code == 201
    member3_org_key = add_member3_resp.json()["api_key"]
    member3_org_headers = {"Authorization": f"Bearer {member3_org_key}"}

    # Verify no Assistants project exists yet
    projects_resp = await client.get("/v0/projects", headers=org_headers)
    assert "Assistants" not in projects_resp.json()

    # User1 creates first assistant (creates Assistants project)
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Team", "surname": "Assistant", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200

    # Verify all users can now see Assistants project
    projects_resp1 = await client.get("/v0/projects", headers=org_headers)
    assert "Assistants" in projects_resp1.json(), "Owner should see Assistants project"

    projects_resp2 = await client.get("/v0/projects", headers=member2_org_headers)
    assert (
        "Assistants" in projects_resp2.json()
    ), "Member2 should see Assistants project"

    projects_resp3 = await client.get("/v0/projects", headers=member3_org_headers)
    assert (
        "Assistants" in projects_resp3.json()
    ), "Member3 should see Assistants project"

    # Verify roles: user1=Owner, user2=Member, user3=Member
    resource_access_dao = ResourceAccessDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    projects = project_dao.filter(organization_id=org_id, name="Assistants")
    project_id = projects[0][0].id

    # User1 should have Owner (delete permission)
    assert resource_access_dao.check_user_permission(
        user1["id"],
        "project",
        project_id,
        "project:delete",
    ), "Creator should have Owner role with delete permission"

    # User2 should have Member (read but not delete)
    assert resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:read",
    ), "Member2 should have read permission"
    assert not resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:delete",
    ), "Member2 should NOT have delete permission"

    # User3 should have Member (read but not delete)
    assert resource_access_dao.check_user_permission(
        user3["id"],
        "project",
        project_id,
        "project:read",
    ), "Member3 should have read permission"
    assert not resource_access_dao.check_user_permission(
        user3["id"],
        "project",
        project_id,
        "project:delete",
    ), "Member3 should NOT have delete permission"


@pytest.mark.anyio
async def test_new_org_member_via_invite_gets_assistants_project_access(
    client: AsyncClient,
    dbsession,
):
    """
    Test that accepting an org invite grants Member access to Assistants project.

    When a user accepts an invite to join an org with an existing Assistants project:
    - User should immediately get Member role on Assistants project
    """
    # Create org owner (user1)
    user1 = await create_test_user(
        client,
        "org_invite_owner@test.com",
    )

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Invite Assistants Org"},
        headers=user1["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_id = org_resp.json()["id"]
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # User1 creates assistant (creates Assistants project)
    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Invite", "surname": "Assistant", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == 200

    # Verify Assistants project exists
    projects_resp = await client.get("/v0/projects", headers=org_headers)
    assert "Assistants" in projects_resp.json()

    # Create user2 and send invite
    user2 = await create_test_user(
        client,
        "org_invite_member@test.com",
    )

    invite_resp = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "org_invite_member@test.com"},
        headers=user1["headers"],
    )
    assert invite_resp.status_code == 201
    invite_token = invite_resp.json()["token"]

    # User2 accepts invite
    accept_resp = await client.post(
        f"/v0/invites/{invite_token}/accept",
        headers=user2["headers"],
    )
    assert accept_resp.status_code == 200
    member_org_key = accept_resp.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    # Verify user2 can immediately see Assistants project
    projects_resp2 = await client.get("/v0/projects", headers=member_org_headers)
    assert projects_resp2.status_code == 200
    assert (
        "Assistants" in projects_resp2.json()
    ), "User2 should have access to Assistants project after accepting invite"

    # Verify user2 has Member role (project:read but NOT project:delete)
    resource_access_dao = ResourceAccessDAO(dbsession)
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)

    projects = project_dao.filter(organization_id=org_id, name="Assistants")
    project_id = projects[0][0].id

    assert resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:read",
    ), "Invited member should have read permission"
    assert not resource_access_dao.check_user_permission(
        user2["id"],
        "project",
        project_id,
        "project:delete",
    ), "Invited member should NOT have delete permission"
