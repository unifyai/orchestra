"""
Tests for assistant creation with create_infra=True.

This module tests the infrastructure creation path including:
- Email creation and watching
- Phone number provisioning
- WhatsApp sender assignment
- PubSub topic creation
- Rollback behavior on failures
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import AssistantCleanupTask
from orchestra.tests.utils import HEADERS


def _mock_get_db_session_generator(real_session):
    """Create a mock get_db_session that returns the real session."""

    def mock_get_db_session(request):
        yield real_session

    return mock_get_db_session


@pytest.fixture
def mock_all_infra(dbsession):
    """
    Mock all infrastructure utilities for create_infra=True testing.
    Yields dict of mocks for test configuration and assertion.
    """
    patches = {
        "create_email": AsyncMock(
            return_value={"user": {"primaryEmail": "testassistant@assistant.unify.ai"}},
        ),
        "watch_email": AsyncMock(return_value={"historyId": "123456"}),
        "create_phone_number": AsyncMock(
            return_value={"phoneNumber": "+15551234567"},
        ),
        "create_pubsub_topic": AsyncMock(return_value={"name": "unity-1"}),
        "delete_email": AsyncMock(return_value={"success": True}),
        "delete_phone_number": AsyncMock(return_value={"success": True}),
        "delete_pubsub_topic": AsyncMock(return_value={"success": True}),
        "process_assistant_cleanup_tasks": AsyncMock(
            return_value={
                "processed": 1,
                "completed": 1,
                "retried": 0,
                "failed": 0,
                "errors": [],
            },
        ),
        "_cleanup_after_assistant_delete": AsyncMock(return_value=None),
        "wake_up_assistant": AsyncMock(return_value=MagicMock(status_code=200)),
        "log_pre_hire_chat": AsyncMock(return_value={"status": "success"}),
    }

    release_pool_vm_mock = AsyncMock(return_value={"success": True})
    wa_pool_mock = AsyncMock(return_value={"pool_number": "+15559876543"})
    wa_register_mock = AsyncMock(return_value={"success": True})
    dc_pool_mock = AsyncMock(
        return_value={
            "pool_number": "123456789012345678",
            "auth_token": "fake-discord-bot-token",
        },
    )
    dc_register_mock = AsyncMock(return_value={"success": True})
    dc_delete_routes_mock = AsyncMock(return_value=0)

    with patch.multiple("orchestra.web.api.assistant.views", **patches):
        with patch(
            "orchestra.web.api.utils.assistant_infra.release_pool_vm",
            release_pool_vm_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.assign_whatsapp_pool_number",
            wa_pool_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.register_whatsapp_sender",
            wa_register_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.assign_discord_pool_bot",
            dc_pool_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.register_discord_bot",
            dc_register_mock,
        ), patch(
            "orchestra.web.api.utils.assistant_infra.delete_discord_routes",
            dc_delete_routes_mock,
        ):
            with patch(
                "orchestra.web.api.assistant.views.settings",
            ) as mock_settings:
                mock_settings.is_staging = True
                with patch(
                    "orchestra.web.api.assistant.views.get_db_session",
                    side_effect=_mock_get_db_session_generator(dbsession),
                ):
                    with patch(
                        "orchestra.web.api.assistant.views.asyncio.sleep",
                        new_callable=AsyncMock,
                    ), patch("orchestra.web.api.assistant.views.time.sleep"):
                        patches["release_pool_vm"] = release_pool_vm_mock
                        patches["assign_whatsapp_pool_number"] = wa_pool_mock
                        patches["register_whatsapp_sender"] = wa_register_mock
                        patches["assign_discord_pool_bot"] = dc_pool_mock
                        patches["register_discord_bot"] = dc_register_mock
                        patches["delete_discord_routes"] = dc_delete_routes_mock
                        yield patches


# =============================================================================
# CATEGORY A: Happy Path Tests (Infra Success)
# =============================================================================


@pytest.mark.anyio
async def test_create_assistant_with_infra_full(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating assistant with full infrastructure: email + phone + whatsapp."""
    payload = {
        "first_name": "InfraFull",
        "surname": "Test",
        "email": "infrafull",
        "user_phone": "+15550001111",
        "user_whatsapp_number": "+15550002222",
        "phone_country": "US",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]

    # Contact provisioning is now done via POST /assistant/{id}/contact
    assert data["email"] is None
    assert data["phone"] is None

    # Only pubsub + wake_up should be called during create
    mock_all_infra["create_pubsub_topic"].assert_called_once()
    mock_all_infra["wake_up_assistant"].assert_called_once()

    # Contact provisioning removed from create
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
    mock_all_infra["register_whatsapp_sender"].assert_not_called()

    # Verify no rollback functions were called
    mock_all_infra["delete_email"].assert_not_called()
    mock_all_infra["delete_phone_number"].assert_not_called()
    mock_all_infra["delete_pubsub_topic"].assert_not_called()


@pytest.mark.anyio
async def test_create_assistant_with_infra_email_only(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating assistant with only email infrastructure."""
    payload = {
        "first_name": "EmailOnly",
        "surname": "Infra",
        "email": "emailonly",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    # Contact provisioning removed from create endpoint
    assert data["email"] is None
    assert data["phone"] is None

    mock_all_infra["create_pubsub_topic"].assert_called_once()

    # Contact provisioning removed from create
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["watch_email"].assert_not_called()
    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
    mock_all_infra["register_whatsapp_sender"].assert_not_called()


@pytest.mark.anyio
async def test_create_assistant_with_infra_phone_only(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating assistant with create_infra=True.
    Contact provisioning (phone/email/whatsapp) is handled separately via
    POST /assistant/{id}/contact, so create only sets up pubsub + wakeup.
    """
    payload = {
        "first_name": "PhoneOnly",
        "surname": "Infra",
        "user_phone": "+15550003333",
        "phone_country": "GB",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    # Contact provisioning is no longer part of create_assistant
    assert data["phone"] is None
    assert data["email"] is None

    mock_all_infra["create_pubsub_topic"].assert_called_once()

    # Phone/email provisioning removed from create endpoint
    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["watch_email"].assert_not_called()


@pytest.mark.anyio
async def test_create_assistant_with_infra_no_email_no_phone(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating assistant with create_infra=True but no email/phone - only pubsub."""
    payload = {
        "first_name": "PubsubOnly",
        "surname": "Infra",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    assert data["phone"] is None
    assert data["email"] is None

    # Only pubsub should be created
    mock_all_infra["create_pubsub_topic"].assert_called_once()
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_phone_number"].assert_not_called()


# =============================================================================
# CATEGORY B: Failure and Rollback Tests
# =============================================================================


@pytest.mark.anyio
async def test_create_infra_email_creation_fails(
    client: AsyncClient,
    mock_all_infra,
):
    """Contact provisioning is now done via POST /assistant/{id}/contact,
    so 'email creation failure' during create_assistant doesn't happen.
    The assistant should be created successfully (email field ignored).
    """
    payload = {
        "first_name": "EmailFail",
        "surname": "Test",
        "email": "emailfail",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    # Create succeeds because email provisioning is no longer part of create
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Email/phone provisioning functions should not be called during create
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_email_watch_fails(
    client: AsyncClient,
    mock_all_infra,
):
    """Email watch is no longer part of create_assistant (moved to contact endpoint).
    The assistant should be created successfully.
    """
    payload = {
        "first_name": "WatchFail",
        "surname": "Test",
        "email": "watchfail",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    # Create succeeds because email/watch provisioning is no longer part of create
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Email provisioning functions should not be called during create
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["watch_email"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_phone_creation_fails(
    client: AsyncClient,
    mock_all_infra,
):
    """Phone provisioning is now done via POST /assistant/{id}/contact.
    The assistant should be created successfully even with phone-related payload.
    """
    payload = {
        "first_name": "PhoneFail",
        "surname": "Test",
        "email": "phonefail",
        "user_phone": "+15550004444",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    # Create succeeds because phone provisioning is no longer part of create
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_whatsapp_fails(
    client: AsyncClient,
    mock_all_infra,
):
    """WhatsApp provisioning is now done via POST /assistant/{id}/contact.
    The assistant should be created successfully.
    """
    payload = {
        "first_name": "WhatsappFail",
        "surname": "Test",
        "email": "whatsappfail",
        "user_phone": "+15550005555",
        "user_whatsapp_number": "+15550006666",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    # Create succeeds because contact provisioning is no longer part of create
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
    mock_all_infra["register_whatsapp_sender"].assert_not_called()
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_pubsub_fails(
    client: AsyncClient,
    mock_all_infra,
):
    """Test that pubsub failure triggers rollback (assistant deletion)."""
    # Configure pubsub to fail
    mock_all_infra["create_pubsub_topic"].return_value = {
        "detail": "PubSub quota exceeded",
    }

    payload = {
        "first_name": "PubsubFail",
        "surname": "Test",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    error_detail = resp.json()["detail"]
    assert "Infrastructure setup failed" in error_detail

    # No contact infra to roll back (contact provisioning removed from create)
    mock_all_infra["delete_email"].assert_not_called()
    mock_all_infra["delete_phone_number"].assert_not_called()
    mock_all_infra["delete_pubsub_topic"].assert_not_called()

    # Verify assistant was deleted from DB
    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assistants = list_resp.json()["info"]
    assert all(a["first_name"] != "PubsubFail" for a in assistants)


# =============================================================================
# CATEGORY C: Rollback Failure Tests
# =============================================================================


@pytest.mark.anyio
async def test_create_infra_rollback_also_fails(
    client: AsyncClient,
    mock_all_infra,
):
    """Test error message when pubsub fails.
    Contact provisioning is no longer part of create, so only pubsub rollback applies.
    """
    # Configure pubsub to fail
    mock_all_infra["create_pubsub_topic"].return_value = {
        "detail": "PubSub creation failed",
    }

    payload = {
        "first_name": "RollbackFail",
        "surname": "Test",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    error_detail = resp.json()["detail"]
    assert "Infrastructure setup failed" in error_detail


@pytest.mark.anyio
async def test_create_infra_multiple_rollback_failures(
    client: AsyncClient,
    mock_all_infra,
):
    """Test pubsub failure with rollback.
    Contact provisioning is no longer part of create, so no contact rollbacks.
    """
    # Configure pubsub to fail
    mock_all_infra["create_pubsub_topic"].return_value = {
        "detail": "PubSub error",
    }

    payload = {
        "first_name": "MultiRollbackFail",
        "surname": "Test",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    error_detail = resp.json()["detail"]
    assert "Infrastructure setup failed" in error_detail


# =============================================================================
# CATEGORY D: Edge Cases
# =============================================================================


@pytest.mark.anyio
async def test_create_infra_with_pre_hire_chat(
    client: AsyncClient,
    mock_all_infra,
):
    """Test that pre_hire_chat is logged alongside infrastructure creation."""
    payload = {
        "first_name": "ChatInfra",
        "surname": "Test",
        "create_infra": True,
        "pre_hire_chat": [
            {"role": "user", "msg": "Hello"},
            {"role": "assistant", "msg": "Hi there!"},
        ],
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Verify pubsub and chat logging happened (email provisioning removed from create)
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()
    mock_all_infra["log_pre_hire_chat"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_email_extracts_local_part(
    client: AsyncClient,
    mock_all_infra,
):
    """Email provisioning is now done via POST /assistant/{id}/contact.
    The create endpoint should succeed and not call create_email.
    """
    payload = {
        "first_name": "LocalPart",
        "surname": "Test",
        "email": "myassistant@example.com",  # Full email provided
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Email provisioning removed from create endpoint
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_default_phone_country(
    client: AsyncClient,
    mock_all_infra,
):
    """Phone provisioning is now done via POST /assistant/{id}/contact.
    The create endpoint should succeed without calling create_phone_number.
    """
    payload = {
        "first_name": "DefaultCountry",
        "surname": "Test",
        "user_phone": "+15550009999",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Phone provisioning removed from create endpoint
    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_assistant_retrievable_after_success(
    client: AsyncClient,
    mock_all_infra,
):
    """Test that assistant is properly retrievable after successful infra creation."""
    payload = {
        "first_name": "Retrievable",
        "surname": "Assistant",
        "create_infra": True,
    }

    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == status.HTTP_200_OK
    agent_id = create_resp.json()["info"]["agent_id"]

    # Verify assistant can be retrieved via list endpoint
    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assert list_resp.status_code == status.HTTP_200_OK

    assistants = list_resp.json()["info"]
    matching = [a for a in assistants if a["agent_id"] == agent_id]
    assert len(matching) == 1

    data = matching[0]
    assert data["first_name"] == "Retrievable"
    # Contact fields are None (provisioned separately via contact endpoint)
    assert data["email"] is None
    assert data["phone"] is None


# =============================================================================
# CATEGORY E: Organization Assistant Infrastructure Tests
# =============================================================================


async def _create_org_with_approved_owner(client: AsyncClient):
    """Helper to create an organization with an approved owner."""
    from orchestra.tests.utils import create_test_user

    # Create and approve owner
    owner = await create_test_user(
        client,
        f"org_infra_owner_{id(client)}@test.com",
    )

    # Create organization
    org_resp = await client.post(
        "/v0/organizations",
        json={"name": f"Infra Test Org {id(client)}"},
        headers=owner["headers"],
    )
    assert org_resp.status_code == status.HTTP_201_CREATED
    org_data = org_resp.json()

    return {
        "owner": owner,
        "org_id": org_data["id"],
        "org_api_key": org_data["api_key"],
        "org_headers": {"Authorization": f"Bearer {org_data['api_key']}"},
    }


@pytest.mark.anyio
async def test_org_assistant_with_infra_full(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating org assistant with create_infra=True.
    Contact provisioning is handled separately via POST /assistant/{id}/contact.
    """
    org_ctx = await _create_org_with_approved_owner(client)

    payload = {
        "first_name": "OrgInfraFull",
        "surname": "Test",
        "create_infra": True,
    }

    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]

    # Verify it's an org assistant
    assert data["organization_id"] == org_ctx["org_id"]

    # Contact fields are None (provisioned separately)
    assert data["email"] is None
    assert data["phone"] is None

    # Only pubsub + wake_up should be called during create
    mock_all_infra["create_pubsub_topic"].assert_called_once()
    mock_all_infra["wake_up_assistant"].assert_called_once()

    # Contact provisioning removed from create
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_phone_number"].assert_not_called()
    mock_all_infra["assign_whatsapp_pool_number"].assert_not_called()
    mock_all_infra["register_whatsapp_sender"].assert_not_called()


@pytest.mark.anyio
async def test_org_assistant_with_infra_email_only(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating org assistant with create_infra=True.
    Contact provisioning is handled via POST /assistant/{id}/contact.
    """
    org_ctx = await _create_org_with_approved_owner(client)

    payload = {
        "first_name": "OrgEmailOnly",
        "surname": "Infra",
        "create_infra": True,
    }

    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    assert data["organization_id"] == org_ctx["org_id"]
    assert data["email"] is None  # Contact provisioning removed from create
    assert data["phone"] is None

    mock_all_infra["create_pubsub_topic"].assert_called_once()
    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_phone_number"].assert_not_called()


@pytest.mark.anyio
async def test_org_assistant_infra_pubsub_fails_rollback(
    client: AsyncClient,
    mock_all_infra,
):
    """Test that org assistant pubsub failure triggers rollback."""
    org_ctx = await _create_org_with_approved_owner(client)

    # Configure pubsub to fail
    mock_all_infra["create_pubsub_topic"].return_value = {
        "detail": "PubSub quota exceeded for org",
    }

    payload = {
        "first_name": "OrgPubsubFail",
        "surname": "Test",
        "create_infra": True,
    }

    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    error_detail = resp.json()["detail"]
    assert "Infrastructure setup failed" in error_detail

    # No contact infra to roll back (contact provisioning removed from create)
    mock_all_infra["delete_email"].assert_not_called()
    mock_all_infra["delete_phone_number"].assert_not_called()


@pytest.mark.anyio
async def test_org_assistant_infra_email_fails_no_rollback_needed(
    client: AsyncClient,
    mock_all_infra,
):
    """Email provisioning is now done via POST /assistant/{id}/contact.
    The org assistant should be created successfully.
    """
    org_ctx = await _create_org_with_approved_owner(client)

    payload = {
        "first_name": "OrgEmailFail",
        "surname": "Test",
        "create_infra": True,
    }

    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    # Create succeeds because email provisioning is no longer part of create
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    mock_all_infra["create_email"].assert_not_called()
    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_org_assistant_with_infra_creates_org_assistants_project(
    client: AsyncClient,
    mock_all_infra,
):
    """Test that org assistant with infra creates org Assistants project."""
    org_ctx = await _create_org_with_approved_owner(client)

    # Verify no Assistants project exists yet
    projects_resp = await client.get("/v0/projects", headers=org_ctx["org_headers"])
    assert projects_resp.status_code == 200
    assert "Assistants" not in projects_resp.json()

    payload = {
        "first_name": "OrgProject",
        "surname": "Creator",
        "email": "orgproject",
        "create_infra": True,
    }

    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    # Verify Assistants project now exists
    projects_resp = await client.get("/v0/projects", headers=org_ctx["org_headers"])
    assert projects_resp.status_code == 200
    assert "Assistants" in projects_resp.json()


@pytest.mark.anyio
async def test_org_assistant_infra_rollback_also_fails(
    client: AsyncClient,
    mock_all_infra,
):
    """Test org assistant pubsub failure.
    Contact provisioning is no longer part of create, so no email rollback.
    """
    org_ctx = await _create_org_with_approved_owner(client)

    # Configure pubsub to fail
    mock_all_infra["create_pubsub_topic"].return_value = {
        "detail": "PubSub org error",
    }

    payload = {
        "first_name": "OrgRollbackFail",
        "surname": "Test",
        "create_infra": True,
    }

    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    error_detail = resp.json()["detail"]
    assert "Infrastructure setup failed" in error_detail

    # No contact infra to roll back
    mock_all_infra["delete_email"].assert_not_called()
    mock_all_infra["delete_phone_number"].assert_not_called()


@pytest.mark.anyio
async def test_org_assistant_retrievable_after_infra_success(
    client: AsyncClient,
    mock_all_infra,
):
    """Test that org assistant is retrievable after successful infra creation."""
    org_ctx = await _create_org_with_approved_owner(client)

    payload = {
        "first_name": "OrgRetrievable",
        "surname": "Assistant",
        "email": "orgretrievable",
        "user_phone": "+15554444444",
        "create_infra": True,
    }

    create_resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    assert create_resp.status_code == status.HTTP_200_OK
    agent_id = create_resp.json()["info"]["agent_id"]

    # Verify assistant can be retrieved via list endpoint
    list_resp = await client.get("/v0/assistant", headers=org_ctx["org_headers"])
    assert list_resp.status_code == status.HTTP_200_OK

    assistants = list_resp.json()["info"]
    matching = [a for a in assistants if a["agent_id"] == agent_id]
    assert len(matching) == 1

    data = matching[0]
    assert data["first_name"] == "OrgRetrievable"
    assert data["organization_id"] == org_ctx["org_id"]
    # Contact fields are None (provisioned separately via contact endpoint)
    assert data["email"] is None
    assert data["phone"] is None


@pytest.mark.anyio
async def test_org_assistant_with_pre_hire_chat_and_infra(
    client: AsyncClient,
    mock_all_infra,
):
    """Test that org assistant pre_hire_chat is logged alongside infrastructure."""
    org_ctx = await _create_org_with_approved_owner(client)

    payload = {
        "first_name": "OrgChatInfra",
        "surname": "Test",
        "email": "orgchatinfra",
        "create_infra": True,
        "pre_hire_chat": [
            {"role": "user", "msg": "Hello org assistant"},
            {"role": "assistant", "msg": "Hi, I'm your org assistant!"},
        ],
    }

    resp = await client.post(
        "/v0/assistant",
        json=payload,
        headers=org_ctx["org_headers"],
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    assert resp.json()["info"]["organization_id"] == org_ctx["org_id"]

    # Verify both infra and chat logging happened
    mock_all_infra["create_pubsub_topic"].assert_called_once()
    mock_all_infra["log_pre_hire_chat"].assert_called_once()

    # Contact provisioning removed from create
    mock_all_infra["create_email"].assert_not_called()


# =============================================================================
# CATEGORY F: Windows VM Infrastructure Tests
# =============================================================================


@pytest.mark.anyio
async def test_create_assistant_with_windows_desktop_mode(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating assistant with desktop_mode=windows stores the mode but does not provision a VM."""
    payload = {
        "first_name": "WindowsPool",
        "surname": "Test",
        "desktop_mode": "windows",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    assert data["desktop_mode"] == "windows"
    assert "desktop_url" not in data

    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_create_assistant_with_ubuntu_desktop_mode(
    client: AsyncClient,
    mock_all_infra,
):
    """Test creating assistant with desktop_mode=ubuntu stores the mode but does not provision a VM."""
    payload = {
        "first_name": "UbuntuPool",
        "surname": "Test",
        "desktop_mode": "ubuntu",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    assert data["desktop_mode"] == "ubuntu"
    assert "desktop_url" not in data

    mock_all_infra["create_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_delete_assistant_with_windows_desktop_mode(
    client: AsyncClient,
    mock_all_infra,
    dbsession,
):
    """Test deleting assistant with desktop_mode=windows queues durable cleanup."""
    payload = {
        "first_name": "DeleteWindowsPool",
        "surname": "Test",
        "desktop_mode": "windows",
        "create_infra": True,
    }

    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == status.HTTP_200_OK, create_resp.json()
    agent_id = create_resp.json()["info"]["agent_id"]

    mock_all_infra["_cleanup_after_assistant_delete"].reset_mock()

    delete_resp = await client.delete(f"/v0/assistant/{agent_id}", headers=HEADERS)
    assert delete_resp.status_code == status.HTTP_200_OK, delete_resp.json()

    cleanup_tasks = (
        dbsession.query(AssistantCleanupTask)
        .filter(AssistantCleanupTask.assistant_id == int(agent_id))
        .all()
    )
    assert cleanup_tasks
    assert cleanup_tasks[-1].desktop_mode == "windows"
    mock_all_infra["_cleanup_after_assistant_delete"].assert_awaited()


@pytest.mark.anyio
async def test_delete_assistant_with_ubuntu_desktop_mode(
    client: AsyncClient,
    mock_all_infra,
    dbsession,
):
    """Test deleting assistant with desktop_mode=ubuntu queues durable cleanup."""
    payload = {
        "first_name": "DeleteUbuntuPool",
        "surname": "Test",
        "desktop_mode": "ubuntu",
        "create_infra": True,
    }

    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == status.HTTP_200_OK, create_resp.json()
    agent_id = create_resp.json()["info"]["agent_id"]

    mock_all_infra["_cleanup_after_assistant_delete"].reset_mock()

    delete_resp = await client.delete(f"/v0/assistant/{agent_id}", headers=HEADERS)
    assert delete_resp.status_code == status.HTTP_200_OK, delete_resp.json()

    cleanup_tasks = (
        dbsession.query(AssistantCleanupTask)
        .filter(AssistantCleanupTask.assistant_id == int(agent_id))
        .all()
    )
    assert cleanup_tasks
    assert cleanup_tasks[-1].desktop_mode == "ubuntu"
    mock_all_infra["_cleanup_after_assistant_delete"].assert_awaited()
