"""
Tests for Contact sync service.

Tests the automatic synchronization of User/Assistant profile fields
(timezone, bio) to Contact logs in the Assistants project.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session
from starlette import status

from orchestra.services.contact_sync_service import ContactSyncService
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Automatically mock assistant infrastructure webhooks and staging for all tests."""
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
        # Patch is_staging to skip credit checks
        mock_settings.is_staging = True

        yield mock_wake_up, mock_reawaken, mock_stop_jobs


@pytest.fixture
def contact_sync_service(dbsession: Session) -> ContactSyncService:
    """Create a ContactSyncService instance."""
    return ContactSyncService(dbsession)


# =============================================================================
# USER TIMEZONE SYNC TESTS
# =============================================================================


@pytest.mark.anyio
async def test_user_timezone_sync_updates_contact_log(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that updating user timezone syncs to Contact logs."""
    # Create user
    user = await create_test_user(client, "tz_sync_user@test.com")

    # Create Assistants project
    project_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    assert project_resp.status_code == 200

    # Create a Contact log with user_name and is_system=True
    log_payload = {
        "project": "Assistants",
        "context": "All/Contacts",
        "entries": [
            {
                "user_name": "Test",  # matches the name from create_test_user
                "is_system": True,
                "timezone": "UTC",
                "bio": "Original bio",
                "contact_id": 1,
            },
        ],
    }
    log_resp = await client.post("/v0/logs", json=log_payload, headers=user["headers"])
    assert log_resp.status_code == 200

    # Update user timezone via API
    update_resp = await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "timezone": "Europe/London",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200

    # Verify Contact log was updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["timezone"] == "Europe/London"


@pytest.mark.anyio
async def test_user_timezone_sync_to_multiple_projects(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that user timezone syncs to both personal and org Assistants projects."""
    user = await create_test_user(client, "multi_proj_tz@test.com")

    # Create personal Assistants project with Contact log
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [{"user_name": "Test", "is_system": True, "timezone": "UTC"}],
        },
        headers=user["headers"],
    )

    # Create org and org Assistants project with Contact log
    org_resp = await client.post(
        "/v0/organizations",  # Note: plural
        json={"name": "Multi TZ Sync Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_api_key = org_resp.json()["api_key"]
    org_headers = {"Authorization": f"Bearer {org_api_key}"}

    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [{"user_name": "Test", "is_system": True, "timezone": "UTC"}],
        },
        headers=org_headers,
    )

    # Update user timezone
    await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "timezone": "Asia/Tokyo",
        },
        headers=ADMIN_HEADERS,
    )

    # Verify both Contact logs were updated
    logs_personal = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    assert logs_personal.json()["logs"][0]["entries"]["timezone"] == "Asia/Tokyo"

    logs_org = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert logs_org.json()["logs"][0]["entries"]["timezone"] == "Asia/Tokyo"


@pytest.mark.anyio
async def test_user_timezone_sync_no_project_no_error(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that timezone sync doesn't fail if no Assistants project exists."""
    user = await create_test_user(client, "no_proj_tz@test.com")

    # Update timezone - should not raise error
    update_resp = await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "timezone": "Europe/Paris",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200


@pytest.mark.anyio
async def test_user_timezone_sync_no_matching_logs(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that timezone sync succeeds even if no matching Contact logs exist."""
    user = await create_test_user(client, "no_match_tz@test.com")

    # Create Assistants project with Contact log for different user_name
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {"user_name": "DifferentUser", "is_system": True, "timezone": "UTC"},
            ],
        },
        headers=user["headers"],
    )

    # Update timezone - should succeed (0 logs updated)
    update_resp = await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "timezone": "Europe/Berlin",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200

    # Verify the other user's log was not changed
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    assert logs_resp.json()["logs"][0]["entries"]["timezone"] == "UTC"


# =============================================================================
# USER BIO SYNC TESTS
# =============================================================================


@pytest.mark.anyio
async def test_user_bio_sync_updates_contact_log(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that updating user bio syncs to Contact logs."""
    user = await create_test_user(client, "bio_sync_user@test.com")

    # Create Assistants project and Contact log
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {
                    "user_name": "Test",
                    "is_system": True,
                    "bio": "Original bio",
                    "contact_id": 1,
                },
            ],
        },
        headers=user["headers"],
    )

    # Update user bio via API
    update_resp = await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "bio": "Updated bio for testing",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200

    # Verify Contact log was updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    assert logs_resp.json()["logs"][0]["entries"]["bio"] == "Updated bio for testing"


@pytest.mark.anyio
async def test_user_bio_and_timezone_sync_together(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that updating both bio and timezone syncs both fields."""
    user = await create_test_user(client, "both_sync_user@test.com")

    # Create Assistants project and Contact log
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {
                    "user_name": "Test",
                    "is_system": True,
                    "timezone": "UTC",
                    "bio": "Old bio",
                    "contact_id": 1,
                },
            ],
        },
        headers=user["headers"],
    )

    # Update both fields
    update_resp = await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "bio": "New bio",
            "timezone": "Pacific/Auckland",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200

    # Verify both fields updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    logs = logs_resp.json()["logs"]
    assert logs[0]["entries"]["bio"] == "New bio"
    assert logs[0]["entries"]["timezone"] == "Pacific/Auckland"


# =============================================================================
# ASSISTANT TIMEZONE SYNC TESTS
# =============================================================================


@pytest.mark.anyio
async def test_assistant_timezone_sync_updates_contact_log(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that updating assistant timezone syncs to Contact logs."""
    user = await create_test_user(client, "asst_tz_sync@test.com", hiring_approved=True)

    # Create assistant
    assistant_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Test",
            "surname": "Bot",
            "timezone": "America/New_York",
            "create_infra": False,
        },
        headers=user["headers"],
    )
    assert assistant_resp.status_code == 200
    agent_id = assistant_resp.json()["info"]["agent_id"]

    # Create Contact log for the assistant (contact_id=0)
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {
                    "_assistant": "TestBot",
                    "contact_id": 0,
                    "timezone": "America/New_York",
                    "bio": "Original assistant bio",
                },
            ],
        },
        headers=user["headers"],
    )

    # Update assistant timezone via API
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"timezone": "Europe/Paris", "create_infra": False},
        headers=user["headers"],
    )
    assert update_resp.status_code == 200

    # Verify Contact log was updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()["logs"]
    # Find the assistant's contact log
    asst_log = next(
        (log for log in logs if log["entries"].get("contact_id") == 0),
        None,
    )
    assert asst_log is not None
    assert asst_log["entries"]["timezone"] == "Europe/Paris"


@pytest.mark.anyio
async def test_assistant_timezone_sync_filters_by_contact_id_zero(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that assistant timezone only syncs to contact_id=0 logs."""
    user = await create_test_user(
        client,
        "asst_tz_filter@test.com",
        hiring_approved=True,
    )

    # Create assistant
    assistant_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Filter",
            "surname": "Bot",
            "timezone": "UTC",
            "create_infra": False,
        },
        headers=user["headers"],
    )
    assert assistant_resp.status_code == 200
    agent_id = assistant_resp.json()["info"]["agent_id"]

    # Create Contact logs - one with contact_id=0, one with contact_id=999
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {"_assistant": "FilterBot", "contact_id": 0, "timezone": "UTC"},
                {"_assistant": "FilterBot", "contact_id": 999, "timezone": "UTC"},
            ],
        },
        headers=user["headers"],
    )

    # Update assistant timezone
    await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"timezone": "Europe/London", "create_infra": False},
        headers=user["headers"],
    )

    # Verify only contact_id=0 was updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    logs = logs_resp.json()["logs"]

    for log in logs:
        if log["entries"].get("contact_id") == 0:
            assert log["entries"].get("timezone") == "Europe/London"
        elif log["entries"].get("contact_id") == 999:
            assert log["entries"].get("timezone") == "UTC"  # Unchanged


# =============================================================================
# ASSISTANT BIO SYNC TESTS
# =============================================================================


@pytest.mark.anyio
async def test_assistant_bio_sync_updates_contact_log(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that updating assistant about syncs to Contact logs as bio."""
    user = await create_test_user(
        client,
        "asst_bio_sync@test.com",
        hiring_approved=True,
    )

    # Create assistant
    assistant_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Bio",
            "surname": "Bot",
            "about": "Original bio",
            "create_infra": False,
        },
        headers=user["headers"],
    )
    assert assistant_resp.status_code == 200
    agent_id = assistant_resp.json()["info"]["agent_id"]

    # Create Contact log for the assistant (contact_id=0)
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {"_assistant": "BioBot", "contact_id": 0, "bio": "Original bio"},
            ],
        },
        headers=user["headers"],
    )

    # Update assistant about via API
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"about": "Updated assistant bio", "create_infra": False},
        headers=user["headers"],
    )
    assert update_resp.status_code == 200

    # Verify Contact log was updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    logs = logs_resp.json()["logs"]
    asst_log = next(
        (log for log in logs if log["entries"].get("contact_id") == 0),
        None,
    )
    assert asst_log is not None
    assert asst_log["entries"]["bio"] == "Updated assistant bio"


@pytest.mark.anyio
async def test_assistant_bio_and_timezone_sync_together(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that updating both about and timezone syncs both fields."""
    user = await create_test_user(
        client,
        "asst_both_sync@test.com",
        hiring_approved=True,
    )

    # Create assistant
    assistant_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Both",
            "surname": "Bot",
            "about": "Old bio",
            "timezone": "UTC",
            "create_infra": False,
        },
        headers=user["headers"],
    )
    assert assistant_resp.status_code == 200
    agent_id = assistant_resp.json()["info"]["agent_id"]

    # Create Contact log
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {
                    "_assistant": "BothBot",
                    "contact_id": 0,
                    "bio": "Old bio",
                    "timezone": "UTC",
                },
            ],
        },
        headers=user["headers"],
    )

    # Update both fields
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={
            "about": "New assistant bio",
            "timezone": "Asia/Singapore",
            "create_infra": False,
        },
        headers=user["headers"],
    )
    assert update_resp.status_code == 200

    # Verify both fields updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    logs = logs_resp.json()["logs"]
    asst_log = next(
        (log for log in logs if log["entries"].get("contact_id") == 0),
        None,
    )
    assert asst_log is not None
    assert asst_log["entries"]["bio"] == "New assistant bio"
    assert asst_log["entries"]["timezone"] == "Asia/Singapore"


# =============================================================================
# ORG ASSISTANT SYNC TESTS
# =============================================================================


@pytest.mark.anyio
async def test_org_assistant_timezone_sync(client: AsyncClient, dbsession: Session):
    """Test that org assistant timezone syncs to org's Assistants project."""
    user = await create_test_user(
        client,
        "org_asst_tz_sync@test.com",
        hiring_approved=True,
    )

    # Create org
    org_resp = await client.post(
        "/v0/organizations",  # Note: plural
        json={"name": "Org Asst TZ Sync Org"},
        headers=user["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()
    org_id = org_data["id"]
    org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    # Explicitly create Assistants project for org (needed for log access)
    project_resp = await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=org_headers,
    )
    assert project_resp.status_code == 200

    # Create org assistant using org API key
    # (middleware sets organization_id from the API key)
    assistant_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Org",
            "surname": "Bot",
            "timezone": "UTC",
            "create_infra": False,
        },
        headers=org_headers,
    )
    assert assistant_resp.status_code == 200
    assert assistant_resp.json()["info"]["organization_id"] == org_id
    agent_id = assistant_resp.json()["info"]["agent_id"]

    # Create Contact log in org's Assistants project
    log_create_resp = await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [{"_assistant": "OrgBot", "contact_id": 0, "timezone": "UTC"}],
        },
        headers=org_headers,
    )
    assert log_create_resp.status_code == 200

    # Update assistant timezone using org API key
    # (must use same API key type that was used to create the assistant)
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"timezone": "America/Los_Angeles", "create_infra": False},
        headers=org_headers,
    )
    assert update_resp.status_code == 200

    # Verify Contact log was updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=org_headers,
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()["logs"]
    asst_log = next(
        (log for log in logs if log["entries"].get("contact_id") == 0),
        None,
    )
    assert asst_log is not None
    assert asst_log["entries"]["timezone"] == "America/Los_Angeles"


# =============================================================================
# EDGE CASE TESTS
# =============================================================================


@pytest.mark.anyio
async def test_sync_with_null_name_fields(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that sync handles users with missing name fields gracefully."""
    # Create user without setting first/last name
    user = await create_test_user(client, "null_name_tz@test.com")

    # Create Assistants project
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )

    # Update timezone - should not raise error even with no name
    update_resp = await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "timezone": "Europe/London",
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200


@pytest.mark.anyio
async def test_sync_sets_null_timezone(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that syncing null timezone works correctly."""
    user = await create_test_user(client, "null_tz_sync@test.com")

    # First set the user's timezone to a non-null value
    await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "timezone": "Europe/Paris",
        },
        headers=ADMIN_HEADERS,
    )

    # Create Assistants project and Contact log with initial timezone
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {"user_name": "Test", "is_system": True, "timezone": "Europe/Paris"},
            ],
        },
        headers=user["headers"],
    )

    # Update user timezone to null (should trigger sync because it changed)
    update_resp = await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "timezone": None,
        },
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200

    # Verify Contact log has null timezone
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    assert logs_resp.json()["logs"][0]["entries"]["timezone"] is None


@pytest.mark.anyio
async def test_sync_only_affects_is_system_true_logs(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that user sync only updates logs where is_system=True."""
    user = await create_test_user(client, "is_system_filter@test.com")

    # Create Assistants project
    await client.post(
        "/v0/project",
        json={"name": "Assistants"},
        headers=user["headers"],
    )

    # Create Contact logs - one with is_system=True, one with is_system=False
    await client.post(
        "/v0/logs",
        json={
            "project": "Assistants",
            "context": "All/Contacts",
            "entries": [
                {"user_name": "Test", "is_system": True, "timezone": "UTC"},
                {"user_name": "Test", "is_system": False, "timezone": "UTC"},
            ],
        },
        headers=user["headers"],
    )

    # Update user timezone
    await client.put(
        "/v0/admin/auth-user",
        json={
            "user_id": user["id"],
            "email": user["email"],
            "name": "Test",
            "timezone": "Europe/Rome",
        },
        headers=ADMIN_HEADERS,
    )

    # Verify only is_system=True was updated
    logs_resp = await client.get(
        "/v0/logs?project=Assistants&context=All/Contacts",
        headers=user["headers"],
    )
    logs = logs_resp.json()["logs"]

    for log in logs:
        if log["entries"].get("is_system") is True:
            assert log["entries"].get("timezone") == "Europe/Rome"
        else:
            assert log["entries"].get("timezone") == "UTC"  # Unchanged


@pytest.mark.anyio
async def test_assistant_sync_no_project_no_error(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that assistant sync doesn't fail if no Assistants project exists."""
    user = await create_test_user(
        client,
        "asst_no_proj@test.com",
        hiring_approved=True,
    )

    # Create assistant (Assistants project auto-created)
    assistant_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "NoProj",
            "surname": "Bot",
            "timezone": "UTC",
            "create_infra": False,
        },
        headers=user["headers"],
    )
    assert assistant_resp.status_code == 200
    agent_id = assistant_resp.json()["info"]["agent_id"]

    # Update timezone - should not raise error even if no Contact logs
    update_resp = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"timezone": "Europe/Paris", "create_infra": False},
        headers=user["headers"],
    )
    assert update_resp.status_code == 200
