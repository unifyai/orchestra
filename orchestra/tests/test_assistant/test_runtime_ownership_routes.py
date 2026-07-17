"""Ownership-scoped runtime routes (user-API-key mirrors of ``/admin/*``).

Assistant pods historically hit ``/admin/*`` with the shared
``ORCHESTRA_ADMIN_KEY``. These tests cover the additive, user-API-key
equivalents: for each route the owner's key succeeds, a different user's key
is rejected, and the admin key still works (``auth_api_key`` admits it as the
``__system__`` caller, which bypasses ownership).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.assistant_workspace_file_access_dao import (
    AssistantWorkspaceFileAccessDAO,
)
from orchestra.db.models.orchestra_models import (
    Assistant,
    SharedPlatformRoute,
    SharedPoolNumber,
    TeamAssistantMembership,
)
from orchestra.tests.utils import (
    ADMIN_HEADERS,
    HEADERS,
    create_test_org,
    create_test_user,
    ensure_assistants_project,
)


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls():
    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield


@pytest.fixture(autouse=True)
def materialization_calls(monkeypatch):
    """Capture scheduled activation sync requests without hitting Communication."""
    from orchestra.services import task_machine_state_service

    calls: list[tuple[dict | None, dict | None]] = []

    def _capture(*, previous_activation, current_activation):
        calls.append((previous_activation, current_activation))

    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_activation_materialization",
        _capture,
    )
    return calls


@pytest.fixture
def org_chat_dispatch_mock(monkeypatch) -> AsyncMock:
    """Capture hosted org-chat dispatches instead of calling adapters."""
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "orchestra.web.api.chat.views.dispatch_chat_best_effort",
        mock,
    )
    return mock


async def _create_assistant(
    client: AsyncClient,
    headers: dict,
    first_name: str = "Runtime",
) -> int:
    resp = await client.post(
        "/v0/assistant",
        json={"first_name": first_name, "surname": "Bot", "create_infra": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    return int(resp.json()["info"]["agent_id"])


async def _other_user(client: AsyncClient, prefix: str) -> dict:
    return await create_test_user(client, f"{prefix}-intruder@test.com")


# =============================================================================
# Inactivity follow-up routes
# =============================================================================


@pytest.mark.anyio
@pytest.mark.parametrize(
    "action",
    ["touch-activity", "opt-out-followups", "opt-in-followups"],
)
async def test_followup_routes_ownership(client: AsyncClient, action: str):
    agent_id = await _create_assistant(client, HEADERS)
    other = await _other_user(client, f"followup-{action}")

    owner_resp = await client.post(
        f"/v0/assistant/{agent_id}/{action}",
        headers=HEADERS,
    )
    assert owner_resp.status_code == 200, owner_resp.json()
    assert owner_resp.json()["rows_updated"] == 1

    other_resp = await client.post(
        f"/v0/assistant/{agent_id}/{action}",
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    admin_resp = await client.post(
        f"/v0/assistant/{agent_id}/{action}",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200, admin_resp.json()

    # The admin route is untouched and keeps working.
    legacy_resp = await client.post(
        f"/v0/admin/assistant/{agent_id}/{action}",
        headers=ADMIN_HEADERS,
    )
    assert legacy_resp.status_code == 200, legacy_resp.json()


@pytest.mark.anyio
async def test_followup_opt_out_toggles_flag(
    client: AsyncClient,
    dbsession: Session,
):
    agent_id = await _create_assistant(client, HEADERS)

    resp = await client.post(
        f"/v0/assistant/{agent_id}/opt-out-followups",
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    assistant = dbsession.get(Assistant, agent_id)
    dbsession.refresh(assistant)
    assert assistant.inactivity_followup_opted_out is True

    resp = await client.post(
        f"/v0/assistant/{agent_id}/opt-in-followups",
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    dbsession.refresh(assistant)
    assert assistant.inactivity_followup_opted_out is False


# =============================================================================
# Secrets read
# =============================================================================


@pytest.mark.anyio
async def test_get_secrets_ownership(client: AsyncClient, dbsession: Session):
    agent_id = await _create_assistant(client, HEADERS)
    other = await _other_user(client, "secrets")

    assistant = dbsession.get(Assistant, agent_id)
    AssistantSecretDAO(dbsession).upsert(
        assistant.user_id,
        agent_id,
        "MY_TOKEN",
        "sekret-value",
    )
    dbsession.commit()

    owner_resp = await client.get(
        f"/v0/assistant/{agent_id}/secrets",
        headers=HEADERS,
    )
    assert owner_resp.status_code == 200, owner_resp.json()
    assert owner_resp.json()["secrets"] == {"MY_TOKEN": "sekret-value"}

    other_resp = await client.get(
        f"/v0/assistant/{agent_id}/secrets",
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    admin_resp = await client.get(
        f"/v0/assistant/{agent_id}/secrets",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200, admin_resp.json()
    assert admin_resp.json()["secrets"] == {"MY_TOKEN": "sekret-value"}


# =============================================================================
# Workspace file access aggregate read
# =============================================================================


@pytest.mark.anyio
async def test_workspace_file_access_ownership(
    client: AsyncClient,
    dbsession: Session,
):
    agent_id = await _create_assistant(client, HEADERS)
    other = await _other_user(client, "wfa")

    AssistantWorkspaceFileAccessDAO(dbsession).upsert(
        agent_id=agent_id,
        provider="google",
        default_allow=True,
        decisions=[],
    )
    dbsession.commit()

    owner_resp = await client.get(
        f"/v0/assistant/{agent_id}/workspace-file-access",
        headers=HEADERS,
    )
    assert owner_resp.status_code == 200, owner_resp.json()
    policies = owner_resp.json()["info"]["policies"]
    assert len(policies) == 1
    assert policies[0]["provider"] == "google"
    assert policies[0]["default_allow"] is True

    other_resp = await client.get(
        f"/v0/assistant/{agent_id}/workspace-file-access",
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    admin_resp = await client.get(
        f"/v0/assistant/{agent_id}/workspace-file-access",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200, admin_resp.json()

    # The admin route response stays identical.
    legacy_resp = await client.get(
        f"/v0/admin/assistant/{agent_id}/workspace-file-access",
        headers=ADMIN_HEADERS,
    )
    assert legacy_resp.status_code == 200, legacy_resp.json()
    assert legacy_resp.json() == owner_resp.json()


# =============================================================================
# Runtime profile PATCH
# =============================================================================


@pytest.mark.anyio
async def test_runtime_profile_patch_ownership(
    client: AsyncClient,
    dbsession: Session,
):
    agent_id = await _create_assistant(client, HEADERS)
    other = await _other_user(client, "profile")

    body = {
        "timezone": "Europe/London",
        "about": "Runtime-managed assistant.",
        "job_title": "Ops",
        "desktop_filesync_sshkey": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc",
        "console_config": {
            "version": "1",
            "layout": {"mode": "standard", "defaultTab": "chat"},
            "theme": {"brandName": "Acme"},
        },
    }
    owner_resp = await client.patch(
        f"/v0/assistant/{agent_id}/runtime-profile",
        json=body,
        headers=HEADERS,
    )
    assert owner_resp.status_code == 200, owner_resp.json()
    assert set(owner_resp.json()["updated_fields"]) == {
        "timezone",
        "about",
        "job_title",
        "desktop_filesync_sshkey",
        "console_config",
    }
    assistant = dbsession.get(Assistant, agent_id)
    dbsession.refresh(assistant)
    assert assistant.timezone == "Europe/London"
    assert assistant.job_title == "Ops"
    assert assistant.desktop_filesync_sshkey.startswith("-----BEGIN")

    other_resp = await client.patch(
        f"/v0/assistant/{agent_id}/runtime-profile",
        json={"timezone": "UTC"},
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    admin_resp = await client.patch(
        f"/v0/assistant/{agent_id}/runtime-profile",
        json={"timezone": "UTC"},
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200, admin_resp.json()

    empty_resp = await client.patch(
        f"/v0/assistant/{agent_id}/runtime-profile",
        json={},
        headers=HEADERS,
    )
    assert empty_resp.status_code == status.HTTP_400_BAD_REQUEST

    # The admin PATCH keeps working unchanged.
    legacy_resp = await client.patch(
        f"/v0/admin/assistant/{agent_id}",
        json={"timezone": "Europe/London"},
        headers=ADMIN_HEADERS,
    )
    assert legacy_resp.status_code == 200, legacy_resp.json()


# =============================================================================
# Update user via assistant
# =============================================================================


@pytest.mark.anyio
async def test_update_user_via_owned_assistant(client: AsyncClient):
    owner = await create_test_user(client, "runtime-update-owner@test.com")
    other = await _other_user(client, "update-user")
    agent_id = await _create_assistant(client, owner["headers"])

    body = {
        "assistant_id": agent_id,
        "target_user_email": owner["email"],
        "timezone": "America/New_York",
        "bio": "Updated through the runtime route.",
    }
    owner_resp = await client.post(
        "/v0/assistant/update-user",
        json=body,
        headers=owner["headers"],
    )
    assert owner_resp.status_code == 200, owner_resp.json()
    assert owner_resp.json()["user_id"] == owner["id"]
    assert owner_resp.json()["assistant_type"] == "personal"

    other_resp = await client.post(
        "/v0/assistant/update-user",
        json=body,
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    mismatch_resp = await client.post(
        "/v0/assistant/update-user",
        json={**body, "target_user_email": "someone-else@test.com"},
        headers=owner["headers"],
    )
    assert mismatch_resp.status_code == status.HTTP_404_NOT_FOUND

    admin_resp = await client.post(
        "/v0/assistant/update-user",
        json=body,
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200, admin_resp.json()

    # The admin route is untouched.
    legacy_resp = await client.post(
        "/v0/admin/assistant/update-user",
        json=body,
        headers=ADMIN_HEADERS,
    )
    assert legacy_resp.status_code == 200, legacy_resp.json()


# =============================================================================
# Desktop file-sync key read
# =============================================================================


@pytest.mark.anyio
async def test_desktop_filesync_key_ownership(
    client: AsyncClient,
    dbsession: Session,
):
    agent_id = await _create_assistant(client, HEADERS)
    other = await _other_user(client, "filesync")

    assistant = dbsession.get(Assistant, agent_id)
    assistant.desktop_filesync_sshkey = "-----BEGIN OPENSSH PRIVATE KEY-----\nxyz"
    dbsession.commit()

    owner_resp = await client.get(
        f"/v0/assistant/{agent_id}/desktop-filesync-key",
        headers=HEADERS,
    )
    assert owner_resp.status_code == 200, owner_resp.json()
    payload = owner_resp.json()
    assert payload["desktop_filesync_sshkey"].startswith("-----BEGIN")
    assert payload["user_desktop_filesync_keys"] == {}

    other_resp = await client.get(
        f"/v0/assistant/{agent_id}/desktop-filesync-key",
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    admin_resp = await client.get(
        f"/v0/assistant/{agent_id}/desktop-filesync-key",
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200, admin_resp.json()


# =============================================================================
# Task machine-state routes
# =============================================================================


async def _seed_task_source(
    client: AsyncClient,
    *,
    user_id: str,
    agent_id: int,
    task_id: int,
) -> int:
    from orchestra.tests.test_log import _create_log, _create_project

    project_resp = await _create_project(client, "Assistants")
    assert project_resp.status_code in (200, 400), project_resp.json()

    entries = {
        "task_id": task_id,
        "instance_id": 0,
        "status": "scheduled",
        "_user_id": user_id,
        "_assistant_id": str(agent_id),
        "schedule": {"start_at": "2026-04-10T09:00:00+00:00"},
        "repeat": [{"unit": "day", "count": 1}],
    }
    source_task = await _create_log(
        client,
        "Assistants",
        context=f"{user_id}/{agent_id}/Tasks",
        entries=entries,
    )
    assert source_task.status_code == 200, source_task.json()
    return source_task.json()["log_event_ids"][0]


def _task_run_payload(agent_id: int, task_id: int, source_task_log_id: int) -> dict:
    return {
        "project_name": "Assistants",
        "run_key": f"offline:{agent_id}:{task_id}:rev-1",
        "assistant_id": str(agent_id),
        "task_id": task_id,
        "source_task_log_id": source_task_log_id,
        "source_type": "scheduled",
        "execution_mode": "offline",
        "activation_revision": "rev-1",
        "state": "pending",
    }


@pytest.mark.anyio
async def test_task_machine_user_routes_full_lifecycle(client: AsyncClient):
    import os

    user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
    agent_id = await _create_assistant(client, HEADERS, first_name="TaskBot")
    source_task_log_id = await _seed_task_source(
        client,
        user_id=user_id,
        agent_id=agent_id,
        task_id=101,
    )
    payload = _task_run_payload(agent_id, 101, source_task_log_id)

    create_resp = await client.post(
        "/v0/task-run/create-or-adopt",
        json=payload,
        headers=HEADERS,
    )
    assert create_resp.status_code == 200, create_resp.json()
    assert create_resp.json()["created"] is True
    run = create_resp.json()["run"]
    assert run["run_key"] == payload["run_key"]

    update_resp = await client.post(
        "/v0/task-run/update",
        json={
            "project_name": "Assistants",
            "assistant_id": str(agent_id),
            "run_key": payload["run_key"],
            "source_task_log_id": source_task_log_id,
            "updates": {"state": "running"},
        },
        headers=HEADERS,
    )
    assert update_resp.status_code == 200, update_resp.json()
    assert update_resp.json()["run"]["state"] == "running"

    latest_post_resp = await client.post(
        "/v0/task-run/latest",
        json={
            "project_name": "Assistants",
            "assistant_id": str(agent_id),
            "task_id": 101,
            "source_task_log_id": source_task_log_id,
        },
        headers=HEADERS,
    )
    assert latest_post_resp.status_code == 200, latest_post_resp.json()
    assert latest_post_resp.json()["run"]["run_key"] == payload["run_key"]

    latest_get_resp = await client.get(
        "/v0/task-run/latest",
        params={
            "project_name": "Assistants",
            "assistant_id": str(agent_id),
            "task_id": 101,
            "source_task_log_id": source_task_log_id,
        },
        headers=HEADERS,
    )
    assert latest_get_resp.status_code == 200, latest_get_resp.json()
    assert latest_get_resp.json()["run"]["run_key"] == payload["run_key"]

    op_create_resp = await client.post(
        "/v0/task-outbound-operation/create-or-adopt",
        json={
            "project_name": "Assistants",
            "operation_key": f"op:{agent_id}:101:0",
            "assistant_id": str(agent_id),
            "task_run_key": payload["run_key"],
            "task_id": 101,
            "source_task_log_id": source_task_log_id,
            "operation_index": 0,
            "method_name": "send_email",
            "medium": "email",
            "target_kind": "contact",
            "status": "pending",
        },
        headers=HEADERS,
    )
    assert op_create_resp.status_code == 200, op_create_resp.json()
    assert op_create_resp.json()["created"] is True

    op_update_resp = await client.post(
        "/v0/task-outbound-operation/update",
        json={
            "project_name": "Assistants",
            "assistant_id": str(agent_id),
            "operation_key": f"op:{agent_id}:101:0",
            "source_task_log_id": source_task_log_id,
            "updates": {"status": "sent"},
        },
        headers=HEADERS,
    )
    assert op_update_resp.status_code == 200, op_update_resp.json()
    assert op_update_resp.json()["operation"]["status"] == "sent"


@pytest.mark.anyio
async def test_task_machine_user_routes_reject_non_owner(client: AsyncClient):
    import os

    user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
    other = await _other_user(client, "taskmachine")
    agent_id = await _create_assistant(client, HEADERS, first_name="TaskBot2")
    source_task_log_id = await _seed_task_source(
        client,
        user_id=user_id,
        agent_id=agent_id,
        task_id=202,
    )
    payload = _task_run_payload(agent_id, 202, source_task_log_id)

    other_resp = await client.post(
        "/v0/task-run/create-or-adopt",
        json=payload,
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    # Admin key bypasses ownership on the user route.
    admin_resp = await client.post(
        "/v0/task-run/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert admin_resp.status_code == 200, admin_resp.json()

    # The admin route is untouched and adopts the same row.
    legacy_resp = await client.post(
        "/v0/admin/task-run/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert legacy_resp.status_code == 200, legacy_resp.json()
    assert legacy_resp.json()["created"] is False


# =============================================================================
# Team messages as an owned assistant
# =============================================================================


@pytest.mark.anyio
async def test_team_message_as_owned_assistant(
    client: AsyncClient,
    dbsession: Session,
    org_chat_dispatch_mock: AsyncMock,
):
    owner = await create_test_user(client, "teamchat-runtime-owner@test.com")
    other = await _other_user(client, "teamchat-runtime")
    org = await create_test_org(client, owner, "Runtime Chat Org")
    await ensure_assistants_project(client, org["headers"])
    team_resp = await client.post(
        f"/v0/organizations/{org['id']}/teams",
        headers=org["headers"],
        json={"name": "Runtime Chat Team"},
    )
    assert team_resp.status_code == status.HTTP_201_CREATED, team_resp.json()
    team = team_resp.json()

    author = Assistant(user_id=owner["id"], first_name="Ada", surname="Author")
    outsider = Assistant(user_id=owner["id"], first_name="Out", surname="Sider")
    dbsession.add_all([author, outsider])
    dbsession.flush()
    dbsession.add(
        TeamAssistantMembership(
            team_id=team["id"],
            assistant_id=author.agent_id,
            added_by=owner["id"],
        ),
    )
    dbsession.commit()

    owner_resp = await client.post(
        f"/v0/assistant/{author.agent_id}/chat/messages",
        headers=owner["headers"],
        json={"team_id": team["id"], "content": "On it."},
    )
    assert owner_resp.status_code == status.HTTP_201_CREATED, owner_resp.json()
    reply = owner_resp.json()
    assert reply["sender_kind"] == "assistant"
    assert reply["sender_assistant_id"] == author.agent_id
    assert reply["content"] == "On it."
    org_chat_dispatch_mock.assert_awaited()

    # A different user's key cannot post through someone else's assistant.
    other_resp = await client.post(
        f"/v0/assistant/{author.agent_id}/chat/messages",
        headers=other["headers"],
        json={"team_id": team["id"], "content": "Should fail"},
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    # An owned assistant that is not on the team is rejected.
    non_member_resp = await client.post(
        f"/v0/assistant/{outsider.agent_id}/chat/messages",
        headers=owner["headers"],
        json={"team_id": team["id"], "content": "Not a member"},
    )
    assert non_member_resp.status_code == status.HTTP_403_FORBIDDEN

    # Admin key works on the user route (system bypass).
    admin_resp = await client.post(
        f"/v0/assistant/{author.agent_id}/chat/messages",
        headers=ADMIN_HEADERS,
        json={"team_id": team["id"], "content": "Admin still fine"},
    )
    assert admin_resp.status_code == status.HTTP_201_CREATED, admin_resp.json()

    # The admin route is untouched.
    legacy_resp = await client.post(
        "/v0/admin/chat/messages",
        headers=ADMIN_HEADERS,
        json={
            "assistant_id": author.agent_id,
            "team_id": team["id"],
            "content": "Admin body path",
        },
    )
    assert legacy_resp.status_code == status.HTTP_201_CREATED, legacy_resp.json()


# =============================================================================
# WhatsApp pending-call-intent routes
# =============================================================================


def _seed_whatsapp_route(
    dbsession: Session,
    *,
    agent_id: int,
    pool_number: str,
    contact_number: str,
) -> None:
    pool = SharedPoolNumber(platform="whatsapp", number=pool_number)
    dbsession.add(pool)
    dbsession.flush()
    dbsession.add(
        SharedPlatformRoute(
            pool_number_id=pool.id,
            contact_number=contact_number,
            assistant_id=agent_id,
        ),
    )
    dbsession.commit()


@pytest.mark.anyio
async def test_whatsapp_pending_call_intent_ownership(
    client: AsyncClient,
    dbsession: Session,
):
    agent_id = await _create_assistant(client, HEADERS, first_name="WaBot")
    sibling_id = await _create_assistant(client, HEADERS, first_name="WaSibling")
    other = await _other_user(client, "whatsapp")

    pool_number = "+15550001111"
    contact_number = "+15552223333"
    _seed_whatsapp_route(
        dbsession,
        agent_id=agent_id,
        pool_number=pool_number,
        contact_number=contact_number,
    )
    params = {"pool_number": pool_number, "contact_number": contact_number}

    store_resp = await client.post(
        f"/v0/assistant/{agent_id}/whatsapp/pending-call-intent",
        json={**params, "context": "Call Bob about the invoice."},
        headers=HEADERS,
    )
    assert store_resp.status_code == 200, store_resp.json()
    assert store_resp.json()["context"] == "Call Bob about the invoice."

    get_resp = await client.get(
        f"/v0/assistant/{agent_id}/whatsapp/pending-call-intent",
        params=params,
        headers=HEADERS,
    )
    assert get_resp.status_code == 200, get_resp.json()
    assert get_resp.json()["context"] == "Call Bob about the invoice."

    # A different user's key is rejected on the assistant ownership check.
    other_resp = await client.get(
        f"/v0/assistant/{agent_id}/whatsapp/pending-call-intent",
        params=params,
        headers=other["headers"],
    )
    assert other_resp.status_code == status.HTTP_404_NOT_FOUND

    # An owned assistant that doesn't hold the route is rejected.
    sibling_resp = await client.get(
        f"/v0/assistant/{sibling_id}/whatsapp/pending-call-intent",
        params=params,
        headers=HEADERS,
    )
    assert sibling_resp.status_code == status.HTTP_403_FORBIDDEN
    sibling_store = await client.post(
        f"/v0/assistant/{sibling_id}/whatsapp/pending-call-intent",
        json={**params, "context": "Hijack attempt"},
        headers=HEADERS,
    )
    assert sibling_store.status_code == status.HTTP_403_FORBIDDEN

    # Admin key works on the user route (system bypass of ownership).
    admin_get = await client.get(
        f"/v0/assistant/{agent_id}/whatsapp/pending-call-intent",
        params=params,
        headers=ADMIN_HEADERS,
    )
    assert admin_get.status_code == 200, admin_get.json()

    # The admin routes are untouched.
    legacy_get = await client.get(
        "/v0/admin/whatsapp/pending-call-intent",
        params=params,
        headers=ADMIN_HEADERS,
    )
    assert legacy_get.status_code == 200, legacy_get.json()
    assert legacy_get.json()["context"] == "Call Bob about the invoice."

    clear_resp = await client.delete(
        f"/v0/assistant/{agent_id}/whatsapp/pending-call-intent",
        params=params,
        headers=HEADERS,
    )
    assert clear_resp.status_code == 200, clear_resp.json()
    assert clear_resp.json() == {"cleared": True}

    cleared_get = await client.get(
        f"/v0/assistant/{agent_id}/whatsapp/pending-call-intent",
        params=params,
        headers=HEADERS,
    )
    assert cleared_get.status_code == status.HTTP_404_NOT_FOUND
