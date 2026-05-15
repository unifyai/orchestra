"""
Tests for admin assistant endpoints:
1. admin_update_user_by_assistant - Update user details via assistant lookup
2. admin_update_assistant - Update assistant details directly (admin bypass)
"""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_COWORKER,
    CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_SPACE,
    AssistantConsoleConfig,
    AssistantSpaceMembership,
    ContactMembership,
    Space,
    User,
)
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
        "orchestra.web.api.assistant.views.process_assistant_cleanup_tasks",
        new_callable=AsyncMock,
    ) as mock_cleanup_tasks, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_cleanup_tasks.return_value = {
            "processed": 1,
            "completed": 1,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_settings.is_staging = True

        yield mock_wake_up, mock_reawaken, mock_cleanup_tasks


def _load_seed_memberships_migration():
    migration_path = (
        Path(__file__).parents[2]
        / "db"
        / "migrations"
        / "versions"
        / "2026-05-05-12-00_seed_personal_contact_memberships.py"
    )
    spec = importlib.util.spec_from_file_location(
        "seed_personal_contact_memberships",
        migration_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
async def test_admin_update_assistant_deploy_env(client: AsyncClient, dbsession):
    owner = await create_test_user(
        client,
        "admin_asst_env@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "AdminEnv",
            "surname": "Update",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"deploy_env": "preview"},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 422


@pytest.mark.anyio
async def test_admin_update_assistant_console_config_creates_row(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "admin_asst_console_config@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Console",
            "surname": "Config",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    console_config = {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
        "tabs": {
            "hidden": ["memory", "secrets"],
            "order": ["dashboards", "chat", "actions", "tasks"],
        },
        "theme": {"brandName": "Midland Heart", "accentColor": "#0057ff"},
    }

    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"console_config": console_config},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200, update_resp.json()
    data = update_resp.json()
    assert data["updated_fields"] == ["console_config"]

    cfg = (
        dbsession.query(AssistantConsoleConfig)
        .filter(AssistantConsoleConfig.assistant_id == agent_id)
        .one()
    )
    assert cfg.version == "1"
    assert cfg.layout_mode == "dashboard-centric"
    assert cfg.layout_default_tab == "dashboards"
    assert cfg.tabs_hidden == ["memory", "secrets"]
    assert cfg.tabs_order == ["dashboards", "chat", "actions", "tasks"]
    assert cfg.theme_brand_name == "Midland Heart"
    assert cfg.theme_accent_color == "#0057ff"


@pytest.mark.anyio
async def test_admin_update_assistant_console_config_updates_existing_row(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "admin_asst_console_config_update@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Console",
            "surname": "Update",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    first_config = {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
        "tabs": {"hidden": ["memory"], "order": ["dashboards", "chat"]},
        "theme": {"brandName": "First", "accentColor": "#111111"},
    }
    second_config = {
        "version": "1",
        "layout": {"mode": "standard", "defaultTab": "chat"},
        "tabs": {"hidden": ["secrets"], "order": ["chat", "tasks"]},
        "theme": {"brandName": "Second", "accentColor": "#222222"},
    }

    first_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"console_config": first_config},
        headers=ADMIN_HEADERS,
    )
    assert first_resp.status_code == 200, first_resp.json()

    second_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"console_config": second_config},
        headers=ADMIN_HEADERS,
    )
    assert second_resp.status_code == 200, second_resp.json()

    rows = (
        dbsession.query(AssistantConsoleConfig)
        .filter(AssistantConsoleConfig.assistant_id == agent_id)
        .all()
    )
    assert len(rows) == 1
    cfg = rows[0]
    assert cfg.layout_mode == "standard"
    assert cfg.layout_default_tab == "chat"
    assert cfg.tabs_hidden == ["secrets"]
    assert cfg.tabs_order == ["chat", "tasks"]
    assert cfg.theme_brand_name == "Second"
    assert cfg.theme_accent_color == "#222222"


@pytest.mark.anyio
async def test_admin_update_assistant_console_config_clears_existing_row(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "admin_asst_console_config_clear@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Console",
            "surname": "Clear",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    console_config = {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
        "tabs": {"hidden": ["memory"]},
    }

    create_config_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"console_config": console_config},
        headers=ADMIN_HEADERS,
    )
    assert create_config_resp.status_code == 200, create_config_resp.json()

    clear_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"console_config": None},
        headers=ADMIN_HEADERS,
    )
    assert clear_resp.status_code == 200, clear_resp.json()
    assert clear_resp.json()["updated_fields"] == ["console_config"]

    rows = (
        dbsession.query(AssistantConsoleConfig)
        .filter(AssistantConsoleConfig.assistant_id == agent_id)
        .all()
    )
    assert rows == []

    list_resp = await client.get("/v0/assistant", headers=owner["headers"])
    assert list_resp.status_code == 200, list_resp.json()
    assistant = [a for a in list_resp.json()["info"] if int(a["agent_id"]) == agent_id][
        0
    ]
    assert assistant["console_config"] is None


@pytest.mark.anyio
async def test_admin_update_assistant_omitted_console_config_leaves_row_unchanged(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "admin_asst_console_config_omit@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Console",
            "surname": "Omit",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    console_config = {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
        "tabs": {"hidden": ["memory"], "order": ["dashboards", "chat"]},
        "theme": {"brandName": "Original", "accentColor": "#111111"},
    }

    config_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"console_config": console_config},
        headers=ADMIN_HEADERS,
    )
    assert config_resp.status_code == 200, config_resp.json()

    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"about": "Updated without touching console config"},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200, update_resp.json()
    assert update_resp.json()["updated_fields"] == ["about"]

    cfg = (
        dbsession.query(AssistantConsoleConfig)
        .filter(AssistantConsoleConfig.assistant_id == agent_id)
        .one()
    )
    assert cfg.layout_mode == "dashboard-centric"
    assert cfg.layout_default_tab == "dashboards"
    assert cfg.tabs_hidden == ["memory"]
    assert cfg.tabs_order == ["dashboards", "chat"]
    assert cfg.theme_brand_name == "Original"
    assert cfg.theme_accent_color == "#111111"


@pytest.mark.anyio
async def test_assistant_list_returns_console_config(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "assistant_list_console_config@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "List",
            "surname": "ConsoleConfig",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    console_config = {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
        "tabs": {"hidden": ["memory", "secrets"]},
        "theme": {"brandName": "Midland Heart"},
    }

    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"console_config": console_config},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200, update_resp.json()

    list_resp = await client.get("/v0/assistant", headers=owner["headers"])
    assert list_resp.status_code == 200, list_resp.json()
    assistant = [a for a in list_resp.json()["info"] if int(a["agent_id"]) == agent_id][
        0
    ]

    assert assistant["console_config"] == {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
        "tabs": {"hidden": ["memory", "secrets"]},
        "theme": {"brandName": "Midland Heart"},
    }


@pytest.mark.anyio
async def test_assistant_without_console_config_returns_null(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "assistant_no_console_config@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "No",
            "surname": "ConsoleConfig",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    list_resp = await client.get("/v0/assistant", headers=owner["headers"])
    assert list_resp.status_code == 200, list_resp.json()
    assistant = [a for a in list_resp.json()["info"] if int(a["agent_id"]) == agent_id][
        0
    ]

    assert assistant["console_config"] is None


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

    When from_fields=first_name,surname,agent_id is specified:
    - Response should contain ONLY the requested fields
    - Order of fields in response doesn't matter
    - Optional fields not requested should NOT be present
    """
    owner = await create_test_user(
        client,
        "fields_multiple@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "MultiField",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    created_agent_id = create_resp.json()["info"]["agent_id"]

    admin_resp = await client.get(
        "/v0/admin/assistant?from_fields=first_name,surname,agent_id",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    body = admin_resp.json()
    results = body["info"]

    our_assistant = next(
        (a for a in results if a.get("agent_id") == created_agent_id),
        None,
    )
    assert our_assistant is not None, "Created assistant not found in results"

    EXPECTED_FIELDS = {"first_name", "surname", "agent_id"}
    assert (
        set(our_assistant.keys()) == EXPECTED_FIELDS
    ), f"Expected {EXPECTED_FIELDS}, got {set(our_assistant.keys())}"
    assert our_assistant["first_name"] == "MultiField"
    assert our_assistant["surname"] == "Test"


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

    Using both agent_id filter and fields parameter:
    - Should filter by agent_id
    - Should return ONLY the requested fields
    """
    owner = await create_test_user(
        client,
        "fields_filter_combo@test.com",
    )

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "FilterCombo",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    admin_resp = await client.get(
        f"/v0/admin/assistant?agent_id={agent_id}&from_fields=first_name",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    assert len(results) == 1, f"Expected 1 result, got {len(results)}"

    EXPECTED_FIELDS = {"first_name"}
    assert (
        set(results[0].keys()) == EXPECTED_FIELDS
    ), f"Expected {EXPECTED_FIELDS}, got {set(results[0].keys())}"
    assert results[0]["first_name"] == "FilterCombo"

    assert "agent_id" not in results[0], "agent_id was not requested in fields"
    assert "email" not in results[0], "email was not requested in fields"


@pytest.mark.anyio
async def test_admin_assistant_projects_contact_ids_from_personal_memberships(
    client: AsyncClient,
    dbsession,
):
    """Assistant reads expose the resolved self and boss contact ids."""

    owner = await create_test_user(
        client,
        "contact-ids-overlay@test.com",
    )
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "ContactIds",
            "surname": "Overlay",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    self_membership = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            ContactMembership.relationship == CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
        )
        .one()
    )
    boss_membership = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            ContactMembership.relationship == CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
        )
        .one()
    )
    self_membership.contact_id = 42
    boss_membership.contact_id = 43
    assert self_membership.authoring_assistant_id == agent_id
    assert boss_membership.authoring_assistant_id == agent_id
    dbsession.commit()

    admin_resp = await client.get(
        "/v0/admin/assistant?"
        f"agent_id={agent_id}&from_fields=agent_id,self_contact_id,boss_contact_id",
        headers=ADMIN_HEADERS,
    )

    assert admin_resp.status_code == 200
    assert admin_resp.json()["info"] == [
        {
            "agent_id": str(agent_id),
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
    ]


@pytest.mark.anyio
async def test_admin_assistant_projects_contact_identity_roots(
    client: AsyncClient,
    dbsession,
    monkeypatch,
):
    """Assistant reads backfill missing space identities before projecting roots."""

    monkeypatch.setattr(
        "orchestra.services.coordinator_service.create_pubsub_topic",
        AsyncMock(return_value={"success": True, "skipped": True}),
    )
    owner = await create_test_user(
        client,
        "contact-identity-roots@test.com",
    )
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "ContactIdentity",
            "surname": "Roots",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    complete_space = Space(
        name="Contact Identity Complete",
        description="Complete identity root used by assistant read tests.",
        owner_user_id=owner["id"],
    )
    incomplete_space = Space(
        name="Contact Identity Incomplete",
        description="Incomplete identity root used by assistant read tests.",
        owner_user_id=owner["id"],
    )
    dbsession.add_all([complete_space, incomplete_space])
    dbsession.flush()
    dbsession.add_all(
        [
            AssistantSpaceMembership(
                assistant_id=agent_id,
                space_id=complete_space.space_id,
                added_by=owner["id"],
            ),
            AssistantSpaceMembership(
                assistant_id=agent_id,
                space_id=incomplete_space.space_id,
                added_by=owner["id"],
            ),
            ContactMembership(
                assistant_id=agent_id,
                contact_id=77,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=complete_space.space_id,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            ),
            ContactMembership(
                assistant_id=agent_id,
                contact_id=78,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=complete_space.space_id,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            ),
            ContactMembership(
                assistant_id=agent_id,
                contact_id=88,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=incomplete_space.space_id,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            ),
        ],
    )
    dbsession.commit()

    admin_resp = await client.get(
        "/v0/admin/assistant?"
        f"agent_id={agent_id}&from_fields=agent_id,contact_identity_roots",
        headers=ADMIN_HEADERS,
    )

    assert admin_resp.status_code == 200
    assert admin_resp.json()["info"] == [
        {
            "agent_id": str(agent_id),
            "contact_identity_roots": [
                {
                    "target_scope": CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                    "target_space_id": None,
                    "self_contact_id": 0,
                    "boss_contact_id": 1,
                },
                {
                    "target_scope": CONTACT_MEMBERSHIP_SCOPE_SPACE,
                    "target_space_id": complete_space.space_id,
                    "self_contact_id": 77,
                    "boss_contact_id": 78,
                },
                {
                    "target_scope": CONTACT_MEMBERSHIP_SCOPE_SPACE,
                    "target_space_id": incomplete_space.space_id,
                    "self_contact_id": 88,
                    "boss_contact_id": 1,
                },
            ],
        },
    ]
    incomplete_space_rows = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
            ContactMembership.target_space_id == incomplete_space.space_id,
        )
        .order_by(ContactMembership.contact_id.asc())
        .all()
    )
    assert [(row.contact_id, row.relationship) for row in incomplete_space_rows] == [
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
        (88, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
    ]


@pytest.mark.anyio
async def test_create_assistant_provisions_personal_contact_memberships(
    client: AsyncClient,
    dbsession,
):
    """Fresh assistants get personal self and boss contact overlays."""

    owner = await create_test_user(
        client,
        "contact-ids-provisioning@test.com",
    )
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "ContactIds",
            "surname": "Provisioned",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    admin_resp = await client.get(
        "/v0/admin/assistant?"
        f"agent_id={agent_id}&from_fields=agent_id,self_contact_id,boss_contact_id",
        headers=ADMIN_HEADERS,
    )

    assert admin_resp.status_code == 200
    assert admin_resp.json()["info"] == [
        {
            "agent_id": str(agent_id),
            "self_contact_id": 0,
            "boss_contact_id": 1,
        },
    ]
    rows = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        )
        .all()
    )
    assert len(rows) == 2
    memberships = {row.relationship: row for row in rows}
    assert {
        relationship: row.contact_id for relationship, row in memberships.items()
    } == {
        CONTACT_MEMBERSHIP_RELATIONSHIP_SELF: 0,
        CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS: 1,
    }
    for row in rows:
        assert row.target_space_id is None
        assert row.should_respond is True
        assert row.can_edit is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "relationship_to_delete",
    [
        CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
        CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    ],
)
async def test_admin_assistant_contact_ids_repair_missing_memberships(
    client: AsyncClient,
    dbsession,
    relationship_to_delete,
):
    """Assistant reads repair required personal overlays before projecting ids."""

    owner = await create_test_user(
        client,
        "contact-ids-missing@test.com",
    )
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "ContactIds",
            "surname": "Missing",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.relationship == relationship_to_delete,
        )
        .delete(synchronize_session=False)
    )
    dbsession.commit()

    admin_resp = await client.get(
        "/v0/admin/assistant?"
        f"agent_id={agent_id}&from_fields=agent_id,self_contact_id,boss_contact_id",
        headers=ADMIN_HEADERS,
    )

    assert admin_resp.status_code == status.HTTP_200_OK
    assert admin_resp.json()["info"] == [
        {
            "agent_id": str(agent_id),
            "self_contact_id": 0,
            "boss_contact_id": 1,
        },
    ]

    rows = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        )
        .all()
    )
    assert {(row.contact_id, row.relationship) for row in rows} == {
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    }


def test_seed_personal_contact_memberships_backfills_missing_relationships(
    dbsession,
    monkeypatch,
):
    """The data repair fills missing overlays without changing existing ids."""

    user = User(
        id="contact-membership-backfill-user",
        email="contact-membership-backfill-user@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    assistant_dao = AssistantDAO(dbsession)

    def create_assistant(first_name: str):
        return assistant_dao.create_assistant(
            user_id=user.id,
            first_name=first_name,
            surname="Backfill",
            age=None,
            nationality=None,
            about=None,
            weekly_limit=None,
            max_parallel=None,
        )

    missing = create_assistant("MissingBoth")
    only_self = create_assistant("OnlySelf")
    only_boss = create_assistant("OnlyBoss")
    complete = create_assistant("Complete")
    wrong_default_self = create_assistant("WrongDefaultSelf")
    wrong_default_boss = create_assistant("WrongDefaultBoss")
    dbsession.add_all(
        [
            ContactMembership(
                assistant_id=only_self.agent_id,
                contact_id=20,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            ),
            ContactMembership(
                assistant_id=only_boss.agent_id,
                contact_id=31,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            ),
            ContactMembership(
                assistant_id=complete.agent_id,
                contact_id=42,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            ),
            ContactMembership(
                assistant_id=complete.agent_id,
                contact_id=43,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            ),
            ContactMembership(
                assistant_id=wrong_default_self.agent_id,
                contact_id=0,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER,
            ),
            ContactMembership(
                assistant_id=wrong_default_boss.agent_id,
                contact_id=1,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_COWORKER,
            ),
        ],
    )
    dbsession.flush()

    migration = _load_seed_memberships_migration()
    operations = Operations(MigrationContext.configure(dbsession.connection()))
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    migration.upgrade()

    relationships_by_assistant: dict[int, dict[str, list[int]]] = {}
    rows = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id.in_(
                [
                    missing.agent_id,
                    only_self.agent_id,
                    only_boss.agent_id,
                    complete.agent_id,
                    wrong_default_self.agent_id,
                    wrong_default_boss.agent_id,
                ],
            ),
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        )
        .order_by(ContactMembership.assistant_id, ContactMembership.relationship)
        .all()
    )
    for row in rows:
        relationships_by_assistant.setdefault(row.assistant_id, {}).setdefault(
            row.relationship,
            [],
        ).append(row.contact_id)

    assert relationships_by_assistant == {
        missing.agent_id: {
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF: [0],
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS: [1],
        },
        only_self.agent_id: {
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF: [20],
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS: [1],
        },
        only_boss.agent_id: {
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF: [0],
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS: [31],
        },
        complete.agent_id: {
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF: [42],
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS: [43],
        },
        wrong_default_self.agent_id: {
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF: [0],
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS: [1],
        },
        wrong_default_boss.agent_id: {
            CONTACT_MEMBERSHIP_RELATIONSHIP_SELF: [0],
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS: [1],
        },
    }


@pytest.mark.anyio
async def test_admin_assistant_contact_ids_tolerates_duplicate_personal_relationships(
    client: AsyncClient,
    dbsession,
):
    """Assistant reads choose the earliest personal relationship row."""

    owner = await create_test_user(
        client,
        "contact-ids-duplicate@test.com",
    )
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "ContactIds",
            "surname": "Duplicate",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    (
        dbsession.query(ContactMembership)
        .filter(ContactMembership.assistant_id == agent_id)
        .delete(synchronize_session=False)
    )
    dbsession.commit()
    dbsession.add_all(
        [
            ContactMembership(
                assistant_id=agent_id,
                contact_id=42,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            ),
            ContactMembership(
                assistant_id=agent_id,
                contact_id=44,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
            ),
            ContactMembership(
                assistant_id=agent_id,
                contact_id=43,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            ),
            ContactMembership(
                assistant_id=agent_id,
                contact_id=45,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            ),
        ],
    )
    dbsession.commit()

    admin_resp = await client.get(
        "/v0/admin/assistant?"
        f"agent_id={agent_id}&from_fields=agent_id,self_contact_id,boss_contact_id",
        headers=ADMIN_HEADERS,
    )

    assert admin_resp.status_code == 200
    assert admin_resp.json()["info"] == [
        {
            "agent_id": str(agent_id),
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
    ]


@pytest.mark.anyio
async def test_admin_create_contact_membership_is_idempotent(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "admin-contact-membership-create@test.com",
    )
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Membership",
            "surname": "Create",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    payload = {
        "contact_id": 91,
        "target_scope": CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        "relationship": CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
        "should_respond": True,
        "response_policy": "",
        "can_edit": True,
    }
    first = await client.post(
        f"/v0/admin/assistant/{agent_id}/contact-memberships",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 200
    assert first.json()["info"]["created"] is True
    assert first.json()["info"]["membership"]["contact_id"] == 91
    assert first.json()["info"]["membership"]["authoring_assistant_id"] == agent_id

    second = await client.post(
        f"/v0/admin/assistant/{agent_id}/contact-memberships",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200
    assert second.json()["info"]["created"] is False
    assert (
        second.json()["info"]["membership"]["id"]
        == first.json()["info"]["membership"]["id"]
    )

    rows = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.contact_id == 91,
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].authoring_assistant_id == agent_id


@pytest.mark.anyio
async def test_admin_delete_contact_memberships_removes_assistant_overlay_rows(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(
        client,
        "admin-contact-membership-delete@test.com",
    )
    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Membership",
            "surname": "Delete",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    dbsession.add(
        ContactMembership(
            assistant_id=agent_id,
            contact_id=92,
            target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
        ),
    )
    dbsession.commit()

    delete_resp = await client.delete(
        f"/v0/admin/assistant/{agent_id}/contact-memberships/92",
        params={"target_scope": CONTACT_MEMBERSHIP_SCOPE_PERSONAL},
        headers=ADMIN_HEADERS,
    )
    assert delete_resp.status_code == 200
    assert delete_resp.json()["info"]["deleted"] == 1
    assert (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.contact_id == 92,
        )
        .count()
        == 0
    )


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
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    resp2 = await client.post(
        "/v0/assistant",
        json={
            "first_name": "AgentFilter",
            "surname": "Two",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert resp1.status_code == 200 and resp2.status_code == 200

    agent_id_1 = resp1.json()["info"]["agent_id"]

    admin_resp = await client.get(
        f"/v0/admin/assistant?agent_id={agent_id_1}&from_fields=first_name,surname",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    results = admin_resp.json()["info"]

    assert (
        len(results) == 1
    ), f"Expected 1 result for agent_id filter, got {len(results)}"

    EXPECTED_FIELDS = {"first_name", "surname"}
    assert (
        set(results[0].keys()) == EXPECTED_FIELDS
    ), f"Expected {EXPECTED_FIELDS}, got {set(results[0].keys())}"
    assert results[0]["first_name"] == "AgentFilter"
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


# =============================================================================
# desktop_filesync_sshkey (internal field)
# =============================================================================


@pytest.mark.anyio
async def test_admin_update_assistant_desktop_filesync_sshkey(
    client: AsyncClient,
    dbsession,
):
    """Test setting desktop_filesync_sshkey via admin PATCH endpoint."""
    owner = await create_test_user(client, "admin_sshkey@test.com")

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "SSHKey",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    ssh_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-key-data\n-----END OPENSSH PRIVATE KEY-----"

    update_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"desktop_filesync_sshkey": ssh_key},
        headers=ADMIN_HEADERS,
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert "desktop_filesync_sshkey" in data["updated_fields"]

    assistant_dao = AssistantDAO(dbsession)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    assert assistant.desktop_filesync_sshkey == ssh_key


@pytest.mark.anyio
async def test_admin_list_returns_desktop_filesync_sshkey(
    client: AsyncClient,
    dbsession,
):
    """Test that admin list endpoint returns desktop_filesync_sshkey when set."""
    owner = await create_test_user(client, "admin_sshkey_list@test.com")

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "SSHKeyList",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = int(create_resp.json()["info"]["agent_id"])

    ssh_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nlist-test-key\n-----END OPENSSH PRIVATE KEY-----"

    # Set the key via admin update
    await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"desktop_filesync_sshkey": ssh_key},
        headers=ADMIN_HEADERS,
    )

    # Verify it appears in admin list
    admin_resp = await client.get(
        "/v0/admin/assistant",
        params={"agent_id": agent_id},
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200
    assistants = admin_resp.json()["info"]
    our = next(a for a in assistants if str(a["agent_id"]) == str(agent_id))
    assert our["desktop_filesync_sshkey"] == ssh_key


@pytest.mark.anyio
async def test_non_admin_does_not_return_desktop_filesync_sshkey(
    client: AsyncClient,
    dbsession,
):
    """Test that regular (non-admin) assistant list does not expose the SSH key."""
    owner = await create_test_user(client, "sshkey_nonadmin@test.com")

    create_resp = await client.post(
        "/v0/assistant",
        json={
            "first_name": "SSHKeyHidden",
            "surname": "Test",
            "create_infra": False,
        },
        headers=owner["headers"],
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["info"]["agent_id"]

    ssh_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nhidden-key\n-----END OPENSSH PRIVATE KEY-----"

    # Set the key via admin
    await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"desktop_filesync_sshkey": ssh_key},
        headers=ADMIN_HEADERS,
    )

    # Fetch via regular list endpoint
    list_resp = await client.get(
        "/v0/assistant",
        headers=owner["headers"],
    )
    assert list_resp.status_code == 200
    assistants = list_resp.json()["info"]
    our = next(a for a in assistants if str(a["agent_id"]) == str(agent_id))
    assert our["desktop_filesync_sshkey"] is None
