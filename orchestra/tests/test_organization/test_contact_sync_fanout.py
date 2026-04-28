"""Wiring tests for the contact sync fan-out after org membership changes.

When an organization's membership changes (invite acceptance, direct member
add, member removal) the set of contacts every org assistant should see
changes too. Orchestra fans out the ``sync_contacts`` Adapters webhook so
Unity can re-derive each affected assistant's Contacts table.

These tests pin the wiring: every membership-mutating endpoint must drive
``fan_out_contact_sync_for_org`` (org-wide) or ``trigger_contact_sync_safe``
(single-assistant transfer) at the right point. We mock those callables and
assert on the call arguments rather than reaching out to real Adapters.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.tests.utils import create_test_user
from orchestra.web.api.utils.assistant_infra import fan_out_contact_sync_for_org


@pytest.fixture
def mocked_infra():
    """Mock all Adapters webhooks but expose contact-sync mocks for assertion."""
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
    ) as mock_bucket_cls, patch(
        "orchestra.web.api.organization.views.fan_out_contact_sync_for_org",
        new_callable=AsyncMock,
    ) as mock_fan_out, patch(
        "orchestra.web.api.assistant.views.trigger_contact_sync_safe",
        new_callable=AsyncMock,
    ) as mock_trigger:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_assistant_cleanup.return_value = {
            "processed": 0,
            "completed": 0,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_org_cleanup.return_value = {
            "processed": 0,
            "completed": 0,
            "retried": 0,
            "failed": 0,
            "errors": [],
        }
        mock_settings.is_staging = True

        bucket = MagicMock()
        bucket.delete_all_assistant_data.return_value = {
            "media_files": 0,
            "call_recordings": 0,
            "message_attachments": 0,
        }
        mock_bucket_cls.return_value = bucket

        yield {"fan_out": mock_fan_out, "trigger": mock_trigger}


@pytest.mark.anyio
async def test_accept_invite_fans_out_contact_sync(
    client: AsyncClient,
    dbsession,
    mocked_infra,
):
    """Accepting an org invite must refresh every org assistant's contacts."""
    owner = await create_test_user(client, "fanout_invite_owner@test.com")
    invitee = await create_test_user(client, "fanout_invitee@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Fan-Out Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    invite_resp = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "fanout_invitee@test.com"},
        headers=owner["headers"],
    )
    token = invite_resp.json()["token"]

    accept_resp = await client.post(
        f"/v0/invites/{token}/accept",
        headers=invitee["headers"],
    )
    assert accept_resp.status_code == status.HTTP_200_OK

    mocked_infra["fan_out"].assert_awaited_once()
    args, kwargs = mocked_infra["fan_out"].call_args
    assert args[0] == org_id

    invite_dao = OrganizationInviteDAO(dbsession)
    assert invite_dao.get_by_token(token) is None


@pytest.mark.anyio
async def test_add_organization_member_fans_out_contact_sync(
    client: AsyncClient,
    dbsession,
    mocked_infra,
):
    """Direct member-add must refresh every org assistant's contacts.

    Mirrors the invite-acceptance fan-out so the admin shortcut path stays
    in sync with the user-facing path.
    """
    owner = await create_test_user(client, "fanout_add_owner@test.com")
    member = await create_test_user(client, "fanout_add_member@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Fan-Out Add Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    add_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == status.HTTP_201_CREATED

    mocked_infra["fan_out"].assert_awaited_once()
    args, _kwargs = mocked_infra["fan_out"].call_args
    assert args[0] == org_id


@pytest.mark.anyio
async def test_remove_organization_member_fans_out_contact_sync(
    client: AsyncClient,
    dbsession,
    mocked_infra,
):
    """Removing a member must refresh remaining assistants' contacts."""
    owner = await create_test_user(client, "fanout_remove_owner@test.com")
    member = await create_test_user(client, "fanout_remove_member@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Fan-Out Remove Org"},
        headers=owner["headers"],
    )
    org_id = org_resp.json()["id"]

    add_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == status.HTTP_201_CREATED
    mocked_infra["fan_out"].reset_mock()

    remove_resp = await client.delete(
        f"/v0/organizations/{org_id}/members/{member['id']}",
        headers=owner["headers"],
    )
    assert remove_resp.status_code == status.HTTP_204_NO_CONTENT

    mocked_infra["fan_out"].assert_awaited_once()
    args, _kwargs = mocked_infra["fan_out"].call_args
    assert args[0] == org_id


@pytest.mark.anyio
async def test_transfer_assistant_to_org_triggers_contact_sync(
    client: AsyncClient,
    dbsession,
    mocked_infra,
):
    """Transferring a personal assistant into an org refreshes its Contacts."""
    user = await create_test_user(client, "fanout_transfer_to_org@test.com")

    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Transfer", "surname": "Bot", "create_infra": False},
        headers=user["headers"],
    )
    assert create_resp.status_code == status.HTTP_200_OK
    agent_id = int(create_resp.json()["info"]["agent_id"])

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Fan-Out Transfer Target Org"},
        headers=user["headers"],
    )
    org_id = org_resp.json()["id"]

    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=user["headers"],
    )
    assert transfer_resp.status_code == status.HTTP_200_OK

    mocked_infra["trigger"].assert_awaited_once()
    args, kwargs = mocked_infra["trigger"].call_args
    assert args[0] == agent_id
    assert "deploy_env" in kwargs


@pytest.mark.anyio
async def test_fan_out_helper_iterates_every_org_assistant_and_tolerates_failures():
    """Helper must trigger contact sync per assistant and not abort on errors."""
    assistant_a = MagicMock(agent_id=101, deploy_env="staging")
    assistant_b = MagicMock(agent_id=202, deploy_env=None)
    assistant_c = MagicMock(agent_id=303, deploy_env="production")

    fake_dao = MagicMock()
    fake_dao.list_all_org_assistants.return_value = [
        assistant_a,
        assistant_b,
        assistant_c,
    ]
    fake_session = MagicMock()

    async def trigger_side_effect(agent_id, deploy_env=None):
        if agent_id == 202:
            raise RuntimeError("simulated webhook failure")
        return {"status": "ok"}

    with patch(
        "orchestra.db.dao.assistant_dao.AssistantDAO",
        return_value=fake_dao,
    ), patch(
        "orchestra.web.api.utils.assistant_infra._trigger_contact_sync",
        new_callable=AsyncMock,
        side_effect=trigger_side_effect,
    ) as mock_trigger:
        await fan_out_contact_sync_for_org(42, fake_session)

    fake_dao.list_all_org_assistants.assert_called_once_with(organization_id=42)
    assert mock_trigger.await_count == 3
    awaited_ids = {call.args[0] for call in mock_trigger.await_args_list}
    assert awaited_ids == {101, 202, 303}
    awaited_envs = {
        call.kwargs.get("deploy_env") for call in mock_trigger.await_args_list
    }
    assert awaited_envs == {"staging", None, "production"}


@pytest.mark.anyio
async def test_transfer_assistant_to_personal_triggers_contact_sync(
    client: AsyncClient,
    dbsession,
    mocked_infra,
):
    """Transferring an org assistant to personal refreshes its Contacts."""
    user = await create_test_user(client, "fanout_transfer_to_personal@test.com")

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Fan-Out Transfer Source Org"},
        headers=user["headers"],
    )
    org_data = org_resp.json()
    org_headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    create_resp = await client.post(
        "/v0/assistant",
        json={"first_name": "Transfer", "surname": "Back", "create_infra": False},
        headers=org_headers,
    )
    assert create_resp.status_code == status.HTTP_200_OK
    agent_id = int(create_resp.json()["info"]["agent_id"])

    transfer_resp = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-personal",
        json={"delete_logs": False},
        headers=org_headers,
    )
    assert transfer_resp.status_code == status.HTTP_200_OK

    mocked_infra["trigger"].assert_awaited_once()
    args, kwargs = mocked_infra["trigger"].call_args
    assert args[0] == agent_id
    assert "deploy_env" in kwargs
