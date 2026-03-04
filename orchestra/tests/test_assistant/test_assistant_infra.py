"""
Tests for assistant creation with create_infra=True.

Infrastructure provisioning (email, phone, WhatsApp, VM, PubSub, wakeup) runs
in a background task after the endpoint returns.  These tests verify the
synchronous part of the endpoint: assistant persists, response is correct, and
the background task is dispatched.  Delete-assistant tests verify that teardown
calls the right infra cleanup functions.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS


@pytest.fixture(scope="function", autouse=True)
async def approve_default_user(client: AsyncClient):
    """Ensures the default test user for this module is approved for hiring."""
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    approve_url = f"/v0/admin/user/{user_id}/assistant-hiring-approval/approved"
    approve_resp = await client.put(approve_url, headers=ADMIN_HEADERS)
    assert (
        approve_resp.status_code == status.HTTP_200_OK
    ), f"Failed to approve default user {user_id}: {approve_resp.json()}"


@pytest.fixture
def mock_all_infra():
    """
    Mock infrastructure utilities and background setup for creation tests.

    Infra provisioning now runs in a background task (_post_create_setup), so
    we mock that as a no-op.  Individual infra function mocks are still needed
    for delete-assistant tests that call them synchronously.
    """
    patches = {
        "_post_create_setup": AsyncMock(),
        "log_pre_hire_chat": AsyncMock(return_value={"status": "success"}),
        "delete_email": AsyncMock(return_value={"success": True}),
        "delete_phone_number": AsyncMock(return_value={"success": True}),
        "delete_pubsub_topic": AsyncMock(return_value={"success": True}),
        "delete_vm": AsyncMock(
            return_value={
                "assistant_id": "123",
                "vm_deleted": True,
                "dns_deleted": True,
                "ip_released": True,
            },
        ),
        "get_social_platforms_costs": AsyncMock(
            return_value={"platforms": {"whatsapp": 0, "phone": 0}},
        ),
    }

    with patch.multiple("orchestra.web.api.assistant.views", **patches):
        with patch(
            "orchestra.web.api.assistant.views.settings",
        ) as mock_settings:
            mock_settings.is_staging = True
            yield patches


# =============================================================================
# CATEGORY A: Happy Path Tests (Creation Success)
# =============================================================================


@pytest.mark.anyio
async def test_create_assistant_with_infra_full(
    client: AsyncClient,
    mock_all_infra,
):
    """Assistant creation succeeds and background setup is dispatched."""
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
    assert data["first_name"] == "InfraFull"
    assert data["email"] == "infrafull"

    mock_all_infra["_post_create_setup"].assert_called_once()


@pytest.mark.anyio
async def test_create_assistant_with_infra_email_only(
    client: AsyncClient,
    mock_all_infra,
):
    """Assistant with only email is created and background setup fires."""
    payload = {
        "first_name": "EmailOnly",
        "surname": "Infra",
        "email": "emailonly",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    assert data["email"] == "emailonly"
    assert data["phone"] is None

    mock_all_infra["_post_create_setup"].assert_called_once()


@pytest.mark.anyio
async def test_create_assistant_with_infra_phone_only(
    client: AsyncClient,
    mock_all_infra,
):
    """Assistant with only phone is created and background setup fires."""
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
    assert data["phone"] is None  # populated by background task
    assert data["email"] is None

    mock_all_infra["_post_create_setup"].assert_called_once()


@pytest.mark.anyio
async def test_create_assistant_with_infra_no_email_no_phone(
    client: AsyncClient,
    mock_all_infra,
):
    """Assistant with create_infra=True but no resources still creates."""
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

    mock_all_infra["_post_create_setup"].assert_called_once()


# =============================================================================
# CATEGORY B: Infra Failures Don't Block Creation
# =============================================================================


@pytest.mark.anyio
async def test_create_infra_failures_dont_block_creation(
    client: AsyncClient,
    mock_all_infra,
):
    """Infra runs in background — the endpoint always returns 200 on DB success."""
    payload = {
        "first_name": "InfraWontFail",
        "surname": "Test",
        "email": "emailfail",
        "user_phone": "+15550004444",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK

    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assistants = list_resp.json()["info"]
    assert any(a["first_name"] == "InfraWontFail" for a in assistants)


# =============================================================================
# CATEGORY C: Edge Cases
# =============================================================================


@pytest.mark.anyio
async def test_create_infra_with_pre_hire_chat(
    client: AsyncClient,
    mock_all_infra,
):
    """Pre-hire chat is logged synchronously alongside background infra dispatch."""
    payload = {
        "first_name": "ChatInfra",
        "surname": "Test",
        "email": "chatinfra",
        "create_infra": True,
        "pre_hire_chat": [
            {"role": "user", "msg": "Hello"},
            {"role": "assistant", "msg": "Hi there!"},
        ],
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    mock_all_infra["log_pre_hire_chat"].assert_called_once()
    mock_all_infra["_post_create_setup"].assert_called_once()


@pytest.mark.anyio
async def test_create_infra_assistant_retrievable_after_success(
    client: AsyncClient,
    mock_all_infra,
):
    """Assistant is retrievable via list endpoint after creation."""
    payload = {
        "first_name": "Retrievable",
        "surname": "Assistant",
        "email": "retrievable",
        "user_phone": "+15551010101",
        "create_infra": True,
    }

    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == status.HTTP_200_OK
    agent_id = create_resp.json()["info"]["agent_id"]

    list_resp = await client.get("/v0/assistant", headers=HEADERS)
    assert list_resp.status_code == status.HTTP_200_OK

    assistants = list_resp.json()["info"]
    matching = [a for a in assistants if a["agent_id"] == agent_id]
    assert len(matching) == 1
    assert matching[0]["first_name"] == "Retrievable"


@pytest.mark.anyio
async def test_local_assistant_skips_background_setup(
    client: AsyncClient,
    mock_all_infra,
):
    """Local assistants skip the background setup task entirely."""
    payload = {
        "first_name": "LocalAssistant",
        "surname": "Test",
        "is_local": True,
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    mock_all_infra["_post_create_setup"].assert_not_called()


# =============================================================================
# CATEGORY D: Organization Assistant Tests
# =============================================================================


async def _create_org_with_approved_owner(client: AsyncClient):
    """Helper to create an organization with an approved owner."""
    from orchestra.tests.utils import create_test_user

    owner = await create_test_user(
        client,
        f"org_infra_owner_{id(client)}@test.com",
    )

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
    """Org assistant creation succeeds and dispatches background setup."""
    org_ctx = await _create_org_with_approved_owner(client)

    payload = {
        "first_name": "OrgInfraFull",
        "surname": "Test",
        "email": "orginfrafull",
        "user_phone": "+15551111111",
        "user_whatsapp_number": "+15552222222",
        "phone_country": "US",
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
    assert data["email"] == "orginfrafull"

    mock_all_infra["_post_create_setup"].assert_called_once()


@pytest.mark.anyio
async def test_org_assistant_with_infra_creates_org_assistants_project(
    client: AsyncClient,
    mock_all_infra,
):
    """Org assistant with infra creates org Assistants project."""
    org_ctx = await _create_org_with_approved_owner(client)

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

    projects_resp = await client.get("/v0/projects", headers=org_ctx["org_headers"])
    assert projects_resp.status_code == 200
    assert "Assistants" in projects_resp.json()


@pytest.mark.anyio
async def test_org_assistant_retrievable_after_creation(
    client: AsyncClient,
    mock_all_infra,
):
    """Org assistant is retrievable via list endpoint after creation."""
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

    list_resp = await client.get("/v0/assistant", headers=org_ctx["org_headers"])
    assert list_resp.status_code == status.HTTP_200_OK

    assistants = list_resp.json()["info"]
    matching = [a for a in assistants if a["agent_id"] == agent_id]
    assert len(matching) == 1
    assert matching[0]["first_name"] == "OrgRetrievable"
    assert matching[0]["organization_id"] == org_ctx["org_id"]


@pytest.mark.anyio
async def test_org_assistant_with_pre_hire_chat_and_infra(
    client: AsyncClient,
    mock_all_infra,
):
    """Org assistant pre-hire chat is logged synchronously."""
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

    mock_all_infra["log_pre_hire_chat"].assert_called_once()


# =============================================================================
# CATEGORY E: VM Creation & Deletion Tests
# =============================================================================


@pytest.mark.anyio
async def test_create_assistant_with_windows_vm(
    client: AsyncClient,
    mock_all_infra,
):
    """Windows VM assistant is created; desktop_mode is persisted."""
    payload = {
        "first_name": "WindowsVM",
        "surname": "Test",
        "desktop_mode": "windows",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    assert data["desktop_mode"] == "windows"

    mock_all_infra["_post_create_setup"].assert_called_once()


@pytest.mark.anyio
async def test_create_assistant_with_ubuntu_vm(
    client: AsyncClient,
    mock_all_infra,
):
    """Ubuntu VM assistant is created; desktop_mode is persisted."""
    payload = {
        "first_name": "UbuntuVM",
        "surname": "Test",
        "desktop_mode": "ubuntu",
        "create_infra": True,
    }

    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == status.HTTP_200_OK, resp.json()

    data = resp.json()["info"]
    assert data["desktop_mode"] == "ubuntu"

    mock_all_infra["_post_create_setup"].assert_called_once()


@pytest.mark.anyio
async def test_delete_assistant_with_windows_vm(
    client: AsyncClient,
    mock_all_infra,
):
    """Deleting a Windows VM assistant calls delete_vm with vm_type=windows."""
    payload = {
        "first_name": "DeleteWindowsVM",
        "surname": "Test",
        "desktop_mode": "windows",
        "create_infra": True,
    }

    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == status.HTTP_200_OK, create_resp.json()
    agent_id = create_resp.json()["info"]["agent_id"]

    mock_all_infra["delete_vm"].reset_mock()
    mock_all_infra["delete_pubsub_topic"].reset_mock()

    delete_resp = await client.delete(f"/v0/assistant/{agent_id}", headers=HEADERS)
    assert delete_resp.status_code == status.HTTP_200_OK, delete_resp.json()

    mock_all_infra["delete_vm"].assert_called_once()
    call_kwargs = mock_all_infra["delete_vm"].call_args.kwargs
    assert call_kwargs["vm_type"] == "windows"
    mock_all_infra["delete_pubsub_topic"].assert_called_once()


@pytest.mark.anyio
async def test_delete_assistant_with_ubuntu_vm(
    client: AsyncClient,
    mock_all_infra,
):
    """Deleting an Ubuntu VM assistant calls delete_vm with vm_type=ubuntu."""
    payload = {
        "first_name": "DeleteUbuntuVM",
        "surname": "Test",
        "desktop_mode": "ubuntu",
        "create_infra": True,
    }

    create_resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert create_resp.status_code == status.HTTP_200_OK, create_resp.json()
    agent_id = create_resp.json()["info"]["agent_id"]

    mock_all_infra["delete_vm"].reset_mock()
    mock_all_infra["delete_pubsub_topic"].reset_mock()

    delete_resp = await client.delete(f"/v0/assistant/{agent_id}", headers=HEADERS)
    assert delete_resp.status_code == status.HTTP_200_OK, delete_resp.json()

    mock_all_infra["delete_vm"].assert_called_once()
    call_kwargs = mock_all_infra["delete_vm"].call_args.kwargs
    assert call_kwargs["vm_type"] == "ubuntu"
    mock_all_infra["delete_pubsub_topic"].assert_called_once()
