"""
Tests for admin assistant endpoints:
1. admin_update_user_by_assistant - Update user details via assistant lookup
2. admin_update_assistant - Update assistant details directly (admin bypass)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Automatically mock assistant infrastructure webhooks for all tests."""
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
        "orchestra.web.api.assistant.views.stop_jobs",
        new_callable=AsyncMock,
    ) as mock_stop_jobs, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_stop_jobs.return_value = MagicMock(status_code=200)
        mock_settings.is_staging = True

        yield mock_wake_up, mock_reawaken, mock_stop_jobs


# =============================================================================
# Admin Update User By Assistant Tests
# =============================================================================


@pytest.mark.anyio
async def test_admin_update_user_personal_assistant(client: AsyncClient, dbsession):
    """
    Test updating user details for a personal assistant's owner.

    This should:
    - Find the personal assistant by ID
    - Match the target_user_email to the owner
    - Update the owner's timezone and bio
    """
    owner = await create_test_user(
        client,
        "admin_update_user_personal@test.com",
        hiring_approved=True,
    )

    # Create a personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Personal",
            "surname": "UserUpdate",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Admin updates the user via assistant lookup
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "admin_update_user_personal@test.com",
            "timezone": "America/New_York",
            "bio": "Updated via admin endpoint",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["info"] == "User updated successfully"
    assert data["user_id"] == owner["id"]
    assert data["email"] == "admin_update_user_personal@test.com"
    assert data["assistant_type"] == "personal"

    # Verify the user was actually updated
    auth_user_dao = AuthUserDAO(dbsession)
    user_row = auth_user_dao.get_by_id(owner["id"])
    assert user_row is not None
    user = user_row[0]
    assert user.timezone == "America/New_York"
    assert user.bio == "Updated via admin endpoint"


@pytest.mark.anyio
async def test_admin_update_user_personal_assistant_email_mismatch(
    client: AsyncClient,
    dbsession,
):
    """
    Test that 404 is returned when target_user_email doesn't match owner.
    """
    owner = await create_test_user(
        client,
        "admin_update_mismatch@test.com",
        hiring_approved=True,
    )

    # Create a personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Mismatch",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to update with wrong email
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "wrong_email@test.com",
            "timezone": "Europe/London",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 404
    assert "does not match" in update_resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_update_user_org_assistant(client: AsyncClient, dbsession):
    """
    Test updating user details for an org assistant's member.

    This should:
    - Find the org assistant by ID
    - List org members and match target_user_email
    - Update the matched member's timezone and bio
    """
    owner = await create_test_user(
        client,
        "admin_update_org_owner@test.com",
        hiring_approved=True,
    )
    member = await create_test_user(
        client,
        "admin_update_org_member@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Admin Update User Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_api_key = org_data["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Add member to organization
    add_member_resp = await client.post(
        f"/v0/organizations/{org_data['id']}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member_resp.status_code == 201

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Org",
            "surname": "UserUpdate",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Admin updates the member via assistant lookup
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "admin_update_org_member@test.com",
            "timezone": "Asia/Tokyo",
            "bio": "Org member bio",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["info"] == "User updated successfully"
    assert data["user_id"] == member["id"]
    assert data["email"] == "admin_update_org_member@test.com"
    assert data["assistant_type"] == "organization"

    # Verify the user was actually updated
    auth_user_dao = AuthUserDAO(dbsession)
    user_row = auth_user_dao.get_by_id(member["id"])
    assert user_row is not None
    user = user_row[0]
    assert user.timezone == "Asia/Tokyo"
    assert user.bio == "Org member bio"


@pytest.mark.anyio
async def test_admin_update_user_org_assistant_member_not_found(
    client: AsyncClient,
    dbsession,
):
    """
    Test that 404 is returned when target_user_email is not in the org.
    """
    owner = await create_test_user(
        client,
        "admin_org_notfound_owner@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Admin Org Not Found Test"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_api_key = org_data["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Org",
            "surname": "NotFound",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to update with email not in org
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "not_in_org@test.com",
            "timezone": "Europe/Paris",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 404
    assert "not found in organization" in update_resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_update_user_assistant_not_found(client: AsyncClient):
    """Test that 404 is returned when assistant_id doesn't exist."""
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": 999999,
            "target_user_email": "any@test.com",
            "timezone": "UTC",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 404
    assert "not found" in update_resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_update_user_invalid_timezone(client: AsyncClient, dbsession):
    """Test that invalid timezone returns 422."""
    owner = await create_test_user(
        client,
        "admin_invalid_tz@test.com",
        hiring_approved=True,
    )

    # Create a personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "InvalidTZ",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to update with invalid timezone
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "admin_invalid_tz@test.com",
            "timezone": "Invalid/Timezone",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 422


@pytest.mark.anyio
async def test_admin_update_user_partial_update(client: AsyncClient, dbsession):
    """Test that partial updates work (only timezone OR bio)."""
    owner = await create_test_user(
        client,
        "admin_partial_update@test.com",
        hiring_approved=True,
    )

    # Create a personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Partial",
            "surname": "Update",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Update only timezone
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "admin_partial_update@test.com",
            "timezone": "Pacific/Auckland",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200

    # Verify timezone was updated
    auth_user_dao = AuthUserDAO(dbsession)
    user_row = auth_user_dao.get_by_id(owner["id"])
    user = user_row[0]
    assert user.timezone == "Pacific/Auckland"

    # Update only bio
    update_resp2 = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "admin_partial_update@test.com",
            "bio": "Only bio updated",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp2.status_code == 200

    # Verify bio was updated and timezone preserved
    dbsession.expire_all()
    user_row = auth_user_dao.get_by_id(owner["id"])
    user = user_row[0]
    assert user.bio == "Only bio updated"
    assert user.timezone == "Pacific/Auckland"  # Should be preserved


@pytest.mark.anyio
async def test_admin_update_user_no_fields(client: AsyncClient, dbsession):
    """Test that request with no fields to update returns 400."""
    owner = await create_test_user(
        client,
        "admin_no_fields@test.com",
        hiring_approved=True,
    )

    # Create a personal assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "NoFields",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to update with no fields
    update_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json={
            "assistant_id": agent_id,
            "target_user_email": "admin_no_fields@test.com",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 400
    assert "No fields to update" in update_resp.json()["detail"]


# =============================================================================
# Admin Update Assistant Tests
# =============================================================================


@pytest.mark.anyio
async def test_admin_update_assistant_timezone(client: AsyncClient, dbsession):
    """Test updating assistant's timezone via admin endpoint."""
    owner = await create_test_user(
        client,
        "admin_asst_tz@test.com",
        hiring_approved=True,
    )

    # Create assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "AdminTZ",
            "surname": "Update",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Admin updates timezone
    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"timezone": "Europe/Berlin"},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["info"] == "Assistant updated successfully"
    assert data["assistant_id"] == agent_id
    assert data["updated_fields"] == ["timezone"]

    # Verify in database
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.timezone == "Europe/Berlin"


@pytest.mark.anyio
async def test_admin_update_assistant_about(client: AsyncClient, dbsession):
    """Test updating assistant's about via admin endpoint."""
    owner = await create_test_user(
        client,
        "admin_asst_about@test.com",
        hiring_approved=True,
    )

    # Create assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "AdminAbout",
            "surname": "Update",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Admin updates about
    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"about": "Admin-set description"},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["info"] == "Assistant updated successfully"
    assert data["updated_fields"] == ["about"]

    # Verify in database
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.about == "Admin-set description"


@pytest.mark.anyio
async def test_admin_update_assistant_both_fields(client: AsyncClient, dbsession):
    """Test updating both timezone and about via admin endpoint."""
    owner = await create_test_user(
        client,
        "admin_asst_both@test.com",
        hiring_approved=True,
    )

    # Create assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "AdminBoth",
            "surname": "Update",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Admin updates both fields
    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={
            "timezone": "Australia/Sydney",
            "about": "Both fields updated",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["info"] == "Assistant updated successfully"
    assert "timezone" in data["updated_fields"]
    assert "about" in data["updated_fields"]

    # Verify in database
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.timezone == "Australia/Sydney"
    assert assistant.about == "Both fields updated"


@pytest.mark.anyio
async def test_admin_update_assistant_not_found(client: AsyncClient):
    """Test that 404 is returned when assistant_id doesn't exist."""
    update_resp = await client.patch(
        "/v0/admin/assistant/999999",
        json={"timezone": "UTC"},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 404
    assert "not found" in update_resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_update_assistant_invalid_timezone(client: AsyncClient, dbsession):
    """Test that invalid timezone returns 422."""
    owner = await create_test_user(
        client,
        "admin_asst_invalid_tz@test.com",
        hiring_approved=True,
    )

    # Create assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "InvalidTZ",
            "surname": "Assistant",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to update with invalid timezone
    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"timezone": "Not/A/Timezone"},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 422


@pytest.mark.anyio
async def test_admin_update_assistant_no_changes(client: AsyncClient, dbsession):
    """Test that request with no fields returns 400."""
    owner = await create_test_user(
        client,
        "admin_asst_no_changes@test.com",
        hiring_approved=True,
    )

    # Create assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "NoChanges",
            "surname": "Assistant",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Try to update with empty body
    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 400
    assert "No fields to update" in update_resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_update_assistant_org_assistant(client: AsyncClient, dbsession):
    """Test that admin can update org assistants without permission checks."""
    owner = await create_test_user(
        client,
        "admin_org_asst_update@test.com",
        hiring_approved=True,
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Admin Org Asst Update Org"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_api_key = org_data["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    # Create org assistant
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "OrgAdmin",
            "surname": "Update",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    # Admin updates org assistant (no permission checks)
    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={
            "timezone": "America/Los_Angeles",
            "about": "Org assistant updated by admin",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200

    # Verify in database
    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.timezone == "America/Los_Angeles"
    assert assistant.about == "Org assistant updated by admin"
