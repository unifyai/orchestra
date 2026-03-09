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
from orchestra.db.dao.user_dao import UserDAO
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
    user_dao = UserDAO(dbsession)
    user_row = user_dao.get_by_id(owner["id"])
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
    )
    member = await create_test_user(
        client,
        "admin_update_org_member@test.com",
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
    user_dao = UserDAO(dbsession)
    user_row = user_dao.get_by_id(member["id"])
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
    user_dao = UserDAO(dbsession)
    user_row = user_dao.get_by_id(owner["id"])
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
    user_row = user_dao.get_by_id(owner["id"])
    user = user_row[0]
    assert user.bio == "Only bio updated"
    assert user.timezone == "Pacific/Auckland"  # Should be preserved


@pytest.mark.anyio
async def test_admin_update_user_no_fields(client: AsyncClient, dbsession):
    """Test that request with no fields to update returns 400."""
    owner = await create_test_user(
        client,
        "admin_no_fields@test.com",
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


@pytest.mark.anyio
async def test_admin_list_assistants_fields_single_email(client: AsyncClient):
    """
    Test that requesting only 'email' field returns objects with only email.

    When from_fields=email is specified:
    - Response should contain objects with ONLY the 'email' key
    - No other fields should be present (not even agent_id, user_id, created_at)
    - Null emails should still be returned as null values
    """
    owner = await create_test_user(
        client,
        "fields_single_email@test.com",
    )

    # Create assistant with email
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "FieldTest",
            "surname": "Single",
            "email": "field.single@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200

    # Request only email field
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=email",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    body = admin_resp.json()
    assert "info" in body
    results = body["info"]
    assert isinstance(results, list)
    assert len(results) >= 1

    # Verify each result has ONLY the email field
    EXPECTED_FIELDS = {"email"}

    for item in results:
        # Should have exactly the expected fields
        assert (
            set(item.keys()) == EXPECTED_FIELDS
        ), f"Expected exactly {EXPECTED_FIELDS}, got {set(item.keys())}"
        # Optional fields not requested should NOT be present
        assert "first_name" not in item, "first_name should not be in response"
        assert "api_key" not in item, "api_key should not be in response"
        assert "user_email" not in item, "user_email should not be in response"
        assert "agent_id" not in item, "agent_id should not be in response"
        assert "user_id" not in item, "user_id should not be in response"
        assert "created_at" not in item, "created_at should not be in response"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_multiple(client: AsyncClient):
    """
    Test requesting multiple fields returns objects with only those fields.

    When from_fields=email,first_name is specified:
    - Response should contain ONLY the requested fields
    - Order of fields in response doesn't matter
    - Optional fields not requested should NOT be present
    """
    owner = await create_test_user(
        client,
        "fields_multiple@test.com",
    )

    # Create assistant with email
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "MultiField",
            "surname": "Test",
            "email": "multi.field@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200

    # Request multiple fields including agent_id so we can find our assistant
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=email,first_name,agent_id",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    body = admin_resp.json()
    results = body["info"]

    # Find our created assistant by email
    our_assistant = next(
        (a for a in results if a.get("email") == "multi.field@example.com"),
        None,
    )
    assert our_assistant is not None, "Created assistant not found in results"

    # Verify it has ONLY the requested fields
    EXPECTED_FIELDS = {"email", "first_name", "agent_id"}
    assert (
        set(our_assistant.keys()) == EXPECTED_FIELDS
    ), f"Expected {EXPECTED_FIELDS}, got {set(our_assistant.keys())}"
    assert our_assistant["email"] == "multi.field@example.com"
    assert our_assistant["first_name"] == "MultiField"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_excludes_expensive_lookups(
    client: AsyncClient,
):
    """
    Test that field selection avoids expensive lookups when those fields aren't requested.

    Fields like 'api_key', 'user_email', 'user_first_name', 'user_last_name'
    require additional database queries. When these fields aren't requested,
    they should not be computed or returned.
    """
    owner = await create_test_user(
        client,
        "fields_no_expensive@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "NoExpensive",
            "surname": "Lookups",
            "email": "no.expensive@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200

    # Request only basic fields that don't require additional queries
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=agent_id,email,phone",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    # Verify expensive fields are NOT in the response
    for item in results:
        assert (
            "api_key" not in item
        ), "api_key requires extra lookup, should not be present"
        assert (
            "user_email" not in item
        ), "user_email requires extra lookup, should not be present"
        assert "user_first_name" not in item, "user_first_name requires extra lookup"
        assert "user_last_name" not in item, "user_last_name requires extra lookup"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_with_filter_combination(
    client: AsyncClient,
):
    """
    Test that field selection works correctly when combined with existing filters.

    Using both email filter and fields parameter:
    - Should filter by email
    - Should return ONLY the requested fields
    """
    owner = await create_test_user(
        client,
        "fields_filter_combo@test.com",
    )

    unique_email = "filter.combo.unique@example.com"
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "FilterCombo",
            "surname": "Test",
            "email": unique_email,
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200

    # Filter by email AND select specific fields
    admin_resp = await client.get(
        f"/v0/admin/assistant?email={unique_email}&from_fields=first_name",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    # Should return exactly one result (the filtered assistant)
    assert len(results) == 1, f"Expected 1 result, got {len(results)}"

    # Result should have ONLY the requested fields
    EXPECTED_FIELDS = {"first_name"}
    assert (
        set(results[0].keys()) == EXPECTED_FIELDS
    ), f"Expected {EXPECTED_FIELDS}, got {set(results[0].keys())}"
    assert results[0]["first_name"] == "FilterCombo"

    # Note: email was used for filtering but NOT requested in fields,
    # so it should NOT be in the response
    assert "email" not in results[0], "email was not requested in fields"
    assert "agent_id" not in results[0], "agent_id was not requested in fields"


@pytest.mark.anyio
async def test_admin_list_assistants_no_fields_returns_full_objects(
    client: AsyncClient,
):
    """
    Test backward compatibility: when 'from_fields' parameter is omitted,
    full AssistantRead objects should be returned (existing behavior).
    """
    owner = await create_test_user(
        client,
        "fields_full_objects@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "FullObject",
            "surname": "Test",
            "email": "full.object@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    created_agent_id = create_resp.json()["info"]["agent_id"]

    # No from_fields parameter - should return full objects
    admin_resp = await client.get(
        "/v0/admin/assistant",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    # Find our created assistant
    our_assistant = next(
        (a for a in results if a.get("agent_id") == created_agent_id),
        None,
    )
    assert our_assistant is not None

    # Full object should have many fields (existing behavior)
    expected_fields = {
        "agent_id",
        "first_name",
        "surname",
        "email",
        "user_id",
        "created_at",
    }
    for field in expected_fields:
        assert field in our_assistant, f"Full object should have '{field}' field"

    # Should also have the expensive lookup fields
    assert "api_key" in our_assistant, "Full object should have api_key"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_null_values_handled(client: AsyncClient):
    """
    Test that null/None field values are properly handled in field selection.

    When an assistant has null email (no email set), requesting from_fields=email
    should return the null value, not skip the record.
    """
    owner = await create_test_user(
        client,
        "fields_null_values@test.com",
    )

    # Create assistant WITHOUT email
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "NullEmail",
            "surname": "Test",
            "create_infra": False,
            # No email field - will be null
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    created_agent_id = create_resp.json()["info"]["agent_id"]

    # Request email field
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=agent_id,email",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    # Find our assistant with null email
    our_assistant = next(
        (a for a in results if a.get("agent_id") == created_agent_id),
        None,
    )
    assert (
        our_assistant is not None
    ), "Assistant with null email should still be in results"

    # email field should be present with null value
    assert "email" in our_assistant, "email field should be present even if null"
    assert our_assistant["email"] is None, "email should be null for this assistant"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_with_spaces_trimmed(client: AsyncClient):
    """
    Test that field names with spaces are properly trimmed.

    from_fields=email, agent_id, first_name (with spaces) should work like
    from_fields=email,agent_id,first_name (without spaces)
    """
    owner = await create_test_user(
        client,
        "fields_spaces@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "SpaceTrim",
            "surname": "Test",
            "email": "space.trim@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    created_agent_id = create_resp.json()["info"]["agent_id"]

    # Request with spaces around field names
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=email, agent_id, first_name",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    # Find our assistant
    our_assistant = next(
        (a for a in results if a.get("agent_id") == created_agent_id),
        None,
    )
    assert our_assistant is not None

    # Should have ONLY the requested fields (spaces trimmed)
    EXPECTED_FIELDS = {"email", "first_name", "agent_id"}
    assert (
        set(our_assistant.keys()) == EXPECTED_FIELDS
    ), f"Expected {EXPECTED_FIELDS}, got {set(our_assistant.keys())}"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_invalid_field_returns_422(
    client: AsyncClient,
):
    """
    Test that requesting a non-existent field returns 422 error.

    Invalid field names should be rejected with a clear error message
    listing the invalid fields and the valid options.
    """
    owner = await create_test_user(
        client,
        "fields_invalid@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "InvalidField",
            "surname": "Test",
            "email": "invalid.field@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200

    # Request a non-existent field mixed with valid ones
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=email,nonexistent_field_xyz,agent_id",
        headers=ADMIN_HEADERS,
    )

    # Should return 422 for invalid field names
    assert admin_resp.status_code == 422, f"Expected 422, got {admin_resp.status_code}"

    # Error message should mention the invalid field
    error_detail = admin_resp.json().get("detail", "")
    assert (
        "nonexistent_field_xyz" in error_detail
    ), f"Error should mention the invalid field name. Got: {error_detail}"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_empty_string_returns_full_objects(
    client: AsyncClient,
):
    """
    Test that an empty from_fields parameter returns full objects.

    from_fields= (empty string) is treated the same as omitting the parameter,
    returning full AssistantRead objects for backward compatibility.
    """
    owner = await create_test_user(
        client,
        "fields_empty_string@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "EmptyFields",
            "surname": "Test",
            "email": "empty.fields@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    created_agent_id = create_resp.json()["info"]["agent_id"]

    # Request with empty from_fields string
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=",
        headers=ADMIN_HEADERS,
    )

    # Should return 200 with full objects (empty string treated as omitted)
    assert admin_resp.status_code == 200, f"Expected 200, got {admin_resp.status_code}"

    results = admin_resp.json()["info"]
    our_assistant = next(
        (a for a in results if a.get("agent_id") == created_agent_id),
        None,
    )
    assert our_assistant is not None

    # Full object should have many fields (same as no from_fields)
    expected_fields = {"agent_id", "first_name", "surname", "email", "user_id"}
    for field in expected_fields:
        assert field in our_assistant, f"Full object should have '{field}' field"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_agent_id_filter_with_field_selection(
    client: AsyncClient,
):
    """
    Test combining agent_id filter with field selection.

    This ensures the filter still works when we're returning partial objects.
    """
    owner = await create_test_user(
        client,
        "fields_agent_filter@test.com",
    )

    # Create two assistants
    resp1 = await client.post(
        "/v0/assistant",
        json={
            "first_name": "AgentFilter",
            "surname": "One",
            "email": "agent.filter.one@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    resp2 = await client.post(
        "/v0/assistant",
        json={
            "first_name": "AgentFilter",
            "surname": "Two",
            "email": "agent.filter.two@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert resp1.status_code == 200 and resp2.status_code == 200

    agent_id_1 = resp1.json()["info"]["agent_id"]

    # Filter by agent_id and select only email and surname
    admin_resp = await client.get(
        f"/v0/admin/assistant?agent_id={agent_id_1}&from_fields=email,surname",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    # Should return exactly one result
    assert (
        len(results) == 1
    ), f"Expected 1 result for agent_id filter, got {len(results)}"

    # Result should have ONLY the requested fields
    EXPECTED_FIELDS = {"email", "surname"}
    assert (
        set(results[0].keys()) == EXPECTED_FIELDS
    ), f"Expected {EXPECTED_FIELDS}, got {set(results[0].keys())}"
    assert results[0]["email"] == "agent.filter.one@example.com"
    assert results[0]["surname"] == "One"


@pytest.mark.anyio
async def test_admin_list_assistants_fields_case_sensitivity_returns_422(
    client: AsyncClient,
):
    """
    Test that field names are case-sensitive.

    'Email' and 'Agent_Id' are not valid field names (should be 'email' and 'agent_id'),
    so the endpoint should return 422.
    """
    owner = await create_test_user(
        client,
        "fields_case@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "CaseSensitive",
            "surname": "Test",
            "email": "case.sensitive@example.com",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200

    # Request with wrong case - these should be treated as invalid fields
    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=Email,Agent_Id",
        headers=ADMIN_HEADERS,
    )

    # Field names are case-sensitive, so 'Email' and 'Agent_Id' are invalid
    assert admin_resp.status_code == 422, f"Expected 422, got {admin_resp.status_code}"

    # Error message should list the invalid fields
    error_detail = admin_resp.json().get("detail", "")
    assert (
        "Email" in error_detail or "Agent_Id" in error_detail
    ), f"Error should mention the invalid field names. Got: {error_detail}"


# =============================================================================
# team_ids in AssistantRead
# =============================================================================


@pytest.mark.anyio
async def test_admin_list_assistant_team_ids_personal(client: AsyncClient):
    """Personal assistants (no org) return empty team_ids."""
    owner = await create_test_user(client, "team_ids_personal@test.com")

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "PersonalTeam",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    admin_resp = await client.get(
        "/v0/admin/assistant",
        params={"agent_id": agent_id},
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    assert len(assistants) >= 1
    our = next(a for a in assistants if str(a["agent_id"]) == str(agent_id))
    assert our["team_ids"] == []


@pytest.mark.anyio
async def test_admin_list_assistant_team_ids_org_no_teams(client: AsyncClient):
    """Org assistant where user has no team memberships returns empty team_ids."""
    owner = await create_test_user(client, "team_ids_org_no_teams@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "TeamIdsNoTeamsOrg"},
        headers=owner["headers"],
    )
    assert org_resp.status_code in [200, 201]
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_api_key = org_data.get("api_key")
    assert org_api_key, "Org should return an API key"

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "OrgNoTeam",
            "surname": "Test",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    admin_resp = await client.get(
        "/v0/admin/assistant",
        params={"agent_id": agent_id},
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    our = next(a for a in assistants if str(a["agent_id"]) == str(agent_id))
    assert our["organization_id"] == org_id
    assert our["team_ids"] == []


@pytest.mark.anyio
async def test_admin_list_assistant_team_ids_with_membership(client: AsyncClient):
    """Org assistant where user belongs to teams returns those team_ids."""
    owner = await create_test_user(client, "team_ids_member@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "TeamIdsMemberOrg"},
        headers=owner["headers"],
    )
    assert org_resp.status_code in [200, 201]
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_api_key = org_data.get("api_key")

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    team1_resp = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Alpha"},
        headers=owner["headers"],
    )
    assert team1_resp.status_code == 201
    team1_id = team1_resp.json()["id"]

    team2_resp = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": "Beta"},
        headers=owner["headers"],
    )
    assert team2_resp.status_code == 201
    team2_id = team2_resp.json()["id"]

    add_resp = await client.post(
        f"/v0/organizations/{org_id}/teams/{team1_id}/members",
        json={"user_ids": [owner["id"]]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == 200

    add_resp2 = await client.post(
        f"/v0/organizations/{org_id}/teams/{team2_id}/members",
        json={"user_ids": [owner["id"]]},
        headers=owner["headers"],
    )
    assert add_resp2.status_code == 200

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "OrgWithTeams",
            "surname": "Test",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    admin_resp = await client.get(
        "/v0/admin/assistant",
        params={"agent_id": agent_id},
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    our = next(a for a in assistants if str(a["agent_id"]) == str(agent_id))
    assert sorted(our["team_ids"]) == sorted([team1_id, team2_id])


@pytest.mark.anyio
async def test_admin_list_assistant_team_ids_skipped_by_from_fields(
    client: AsyncClient,
):
    """When from_fields does not include team_ids, the field is still present but empty."""
    owner = await create_test_user(client, "team_ids_skip@test.com")

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "SkipTeamIds",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    admin_resp = await client.get(
        "/v0/admin/assistant",
        params={"agent_id": agent_id, "from_fields": "agent_id,email"},
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    our = next(a for a in assistants if str(a["agent_id"]) == str(agent_id))
    assert "team_ids" not in our


@pytest.mark.anyio
async def test_admin_list_assistant_team_ids_requested_via_from_fields(
    client: AsyncClient,
):
    """When from_fields includes team_ids, it is resolved and returned."""
    owner = await create_test_user(client, "team_ids_requested@test.com")

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "RequestTeamIds",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    admin_resp = await client.get(
        "/v0/admin/assistant",
        params={"agent_id": agent_id, "from_fields": "agent_id,team_ids"},
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    our = next(a for a in assistants if str(a["agent_id"]) == str(agent_id))
    assert "team_ids" in our
    assert our["team_ids"] == []
