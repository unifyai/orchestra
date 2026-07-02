"""Endpoint tests for Coordinator provisioning and lifecycle contracts."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from scripts.ensure_test_user_coordinator import ensure_test_user_coordinator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    Assistant,
    AssistantSecret,
    BillingAccount,
    ContactMembership,
    Context,
    LogEvent,
    LogEventContext,
    Organization,
    Project,
    User,
)
from orchestra.services import coordinator_service as svc
from orchestra.services.coordinator_service import (
    COORDINATOR_DEFAULT_FIRST_NAME,
    COORDINATOR_DEFAULT_JOB_TITLE,
)
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS, create_test_user

EXPECTED_COORDINATOR_DEFAULT_NATIONALITY = "United States"
EXPECTED_COORDINATOR_DEFAULT_DESKTOP_MODE = "ubuntu"


@pytest.fixture(autouse=True)
def coordinator_pubsub_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Coordinator tests inside Orchestra's API/database boundary."""
    monkeypatch.setattr(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        AsyncMock(return_value=MagicMock(status_code=200)),
    )
    monkeypatch.setattr(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {})),
    )
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.delete_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.services.coordinator_service.create_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.web.api.users.views.delete_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.db.dao.log_event_dao.LogEventDAO.bucket_service_factory",
        MagicMock(),
    )


async def _create_user(client: AsyncClient, suffix: str) -> dict:
    return await create_test_user(client, f"coordinator-{suffix}@test.com")


async def _create_org(
    client: AsyncClient,
    owner: dict,
    suffix: str,
) -> dict:
    response = await client.post(
        "/v0/organizations",
        json={"name": f"Coordinator Org {suffix}"},
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    organization_payload = response.json()
    coordinator_response = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        params={"organization_id": organization_payload["id"]},
        headers=owner["headers"],
    )
    assert coordinator_response.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, coordinator_response.json()
    return {
        **organization_payload,
        "_organization_response": organization_payload,
        "coordinator_id": coordinator_response.json()["coordinator_id"],
    }


def _assistant_context_name(coordinator: Assistant, suffix: str) -> str:
    return f"{coordinator.user_id}/{coordinator.agent_id}/{suffix}"


@pytest.mark.anyio
async def test_personal_coordinator_endpoint_repairs_existing_pubsub(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await _create_user(client, "repair-existing-pubsub")
    coordinator = dbsession.scalars(
        select(Assistant).where(
            Assistant.user_id == owner["id"],
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        ),
    ).one()
    create_pubsub_topic = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(
        "orchestra.services.coordinator_service.create_pubsub_topic",
        create_pubsub_topic,
    )

    response = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json() == {"coordinator_id": str(coordinator.agent_id)}
    create_pubsub_topic.assert_awaited_once_with(str(coordinator.agent_id))


def _assistants_project(
    dbsession: Session,
    *,
    coordinator: Assistant,
) -> Project:
    criteria = [
        Project.name == "Assistants",
    ]
    if coordinator.organization_id is None:
        criteria.extend(
            [
                Project.user_id == coordinator.user_id,
                Project.organization_id.is_(None),
            ],
        )
    else:
        criteria.append(
            Project.organization_id == coordinator.organization_id,
        )
    return dbsession.scalar(
        select(Project).where(*criteria),
    )


def _context(
    dbsession: Session,
    *,
    project: Project,
    name: str,
) -> Context | None:
    return dbsession.scalar(
        select(Context).where(Context.project_id == project.id, Context.name == name),
    )


def _insert_log(
    dbsession: Session,
    *,
    project: Project,
    context_name: str,
    data: dict,
    created_at: datetime | None = None,
) -> None:
    context = _context(dbsession, project=project, name=context_name)
    if context is None:
        context = Context(project_id=project.id, name=context_name)
        dbsession.add(context)
        dbsession.flush()
    log_event = LogEvent(owner_key="sys", project_id=project.id, data=data)
    if created_at is not None:
        log_event.created_at = created_at
    dbsession.add(log_event)
    dbsession.flush()
    dbsession.add(
        LogEventContext(
            owner_key="sys",
            project_id=project.id,
            log_event_id=log_event.id,
            context_id=context.id,
        ),
    )
    dbsession.flush()


def _context_logs(
    dbsession: Session,
    *,
    context: Context,
) -> list[LogEvent]:
    return dbsession.scalars(
        select(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .where(LogEventContext.context_id == context.id)
        .order_by(LogEvent.id.asc()),
    ).all()


def _personal_memberships(
    dbsession: Session,
    *,
    assistant_id: int,
) -> list[ContactMembership]:
    return dbsession.scalars(
        select(ContactMembership)
        .where(
            ContactMembership.assistant_id == assistant_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        )
        .order_by(ContactMembership.contact_id.asc()),
    ).all()


def _assert_owner_contact_row(
    dbsession: Session,
    *,
    coordinator: Assistant,
    owner_user_id: str,
) -> LogEvent:
    owner = dbsession.get(User, owner_user_id)
    assert owner is not None
    project = _assistants_project(dbsession, coordinator=coordinator)
    contacts = _context(
        dbsession,
        project=project,
        name=_assistant_context_name(coordinator, "Contacts"),
    )
    assert contacts is not None
    contact_logs = [
        log
        for log in _context_logs(dbsession, context=contacts)
        if log.data.get("contact_id") == 1
    ]
    assert len(contact_logs) == 1
    contact_data = contact_logs[0].data
    assert contact_data["first_name"] == owner.name
    assert contact_data["surname"] == owner.last_name
    assert contact_data["email_address"] == owner.email
    assert contact_data["is_system"] is True
    assert contact_data["should_respond"] is True
    assert contact_data["response_policy"]
    return contact_logs[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("trigger_step_id", "reply_step_id", "medium"),
    [
        ("email-reference", "email-reply", "email"),
        ("sms-reference", "sms-message", "sms_message"),
        ("whatsapp-message-reference", "whatsapp-message", "whatsapp_message"),
    ],
)
async def test_onboarding_reply_requires_stamped_outbound_before_user_reply(
    client: AsyncClient,
    dbsession: Session,
    trigger_step_id: str,
    reply_step_id: str,
    medium: str,
) -> None:
    owner = await _create_user(client, f"reply-proof-{reply_step_id}")
    coordinator = dbsession.scalars(
        select(Assistant).where(
            Assistant.user_id == owner["id"],
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        ),
    ).one()
    project = _assistants_project(dbsession, coordinator=coordinator)
    context_name = _assistant_context_name(coordinator, "Transcripts")
    base = datetime(2026, 1, 1, 12, 0, 0)

    def progress() -> list[str]:
        return svc.derive_onboarding_progress(
            dbsession,
            coordinator=coordinator,
            state={"onboarding_active": True},
        )

    def insert_message(
        *,
        role: str,
        content: str,
        offset: int,
        metadata: dict | None = None,
    ) -> None:
        is_assistant = role == "assistant"
        created_at = base + timedelta(seconds=offset)
        _insert_log(
            dbsession,
            project=project,
            context_name=context_name,
            created_at=created_at,
            data={
                "medium": medium,
                "sender_id": (
                    svc.PERSONAL_SELF_CONTACT_ID
                    if is_assistant
                    else svc.PERSONAL_BOSS_CONTACT_ID
                ),
                "receiver_ids": [
                    (
                        svc.PERSONAL_BOSS_CONTACT_ID
                        if is_assistant
                        else svc.PERSONAL_SELF_CONTACT_ID
                    ),
                ],
                "timestamp": created_at.isoformat(),
                "content": content,
                **({"metadata": metadata} if metadata else {}),
            },
        )

    insert_message(role="user", content="A stray earlier reply", offset=1)
    assert reply_step_id not in progress()

    insert_message(role="assistant", content="Template or untagged outbound", offset=2)
    insert_message(role="user", content="Sure", offset=3)
    derived = progress()
    assert trigger_step_id not in derived
    assert reply_step_id not in derived

    insert_message(
        role="assistant",
        content="Tagged onboarding clue",
        offset=4,
        metadata={"onboarding_trigger_step_id": trigger_step_id},
    )
    derived = progress()
    assert trigger_step_id in derived
    assert reply_step_id not in derived

    insert_message(role="user", content="My guess after the clue", offset=5)
    derived = progress()
    assert trigger_step_id in derived
    assert reply_step_id in derived


def _assert_coordinator_provisioned(
    dbsession: Session,
    *,
    org_data: dict,
    owner_user_id: str,
) -> None:
    coordinator_id = int(org_data["coordinator_id"])

    coordinator = dbsession.get(Assistant, coordinator_id)
    assert coordinator is not None
    assert coordinator.is_coordinator is True
    assert coordinator.organization_id == org_data["id"]
    assert coordinator.user_id == owner_user_id
    assert coordinator.nationality == EXPECTED_COORDINATOR_DEFAULT_NATIONALITY
    assert coordinator.desktop_mode == EXPECTED_COORDINATOR_DEFAULT_DESKTOP_MODE
    assert coordinator.about == ""
    assert coordinator.first_name == COORDINATOR_DEFAULT_FIRST_NAME
    assert coordinator.job_title == COORDINATOR_DEFAULT_JOB_TITLE
    only_org_coordinator = dbsession.scalar(
        select(Assistant).where(
            Assistant.organization_id == org_data["id"],
            Assistant.is_coordinator.is_(True),
        ),
    )
    assert only_org_coordinator is not None
    assert only_org_coordinator.agent_id == coordinator.agent_id

    resource_access_dao = ResourceAccessDAO(dbsession)
    assert resource_access_dao.check_user_permission(
        owner_user_id,
        "assistant",
        coordinator.agent_id,
        "assistant:write",
    )
    _assert_owner_contact_row(
        dbsession,
        coordinator=coordinator,
        owner_user_id=owner_user_id,
    )


@pytest.mark.anyio
async def test_create_organization_provisions_coordinator_without_implicit_space(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Organization creation provisions the Coordinator lifecycle surfaces only."""
    owner = await _create_user(client, "org-provision")

    org_data = await _create_org(client, owner, "provision")
    assert "coordinator_id" not in org_data["_organization_response"]

    _assert_coordinator_provisioned(
        dbsession,
        org_data=org_data,
        owner_user_id=owner["id"],
    )


@pytest.mark.anyio
async def test_admin_create_organization_provisions_coordinator_without_implicit_space(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Admin organization creation provisions the same Coordinator surfaces."""
    owner = await _create_user(client, "admin-org-provision")

    response = await client.post(
        "/v0/admin/organizations",
        json={
            "name": "Coordinator Admin Org provision",
            "creator_user_id": owner["id"],
        },
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == status.HTTP_201_CREATED, response.json()
    org_data = response.json()
    assert "coordinator_id" not in org_data
    coordinator_response = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        params={"organization_id": org_data["id"]},
        headers=owner["headers"],
    )
    assert coordinator_response.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, coordinator_response.json()
    org_data["coordinator_id"] = coordinator_response.json()["coordinator_id"]

    _assert_coordinator_provisioned(
        dbsession,
        org_data=org_data,
        owner_user_id=owner["id"],
    )


@pytest.mark.anyio
async def test_transcript_seed_rejects_non_empty_history(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Transcript seeding fails with conflict when history is not empty."""
    owner = await _create_user(client, "seed")
    org_data = await _create_org(client, owner, "seed")
    coordinator_id = int(org_data["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    project = _assistants_project(dbsession, coordinator=coordinator)
    _insert_log(
        dbsession,
        project=project,
        context_name=_assistant_context_name(coordinator, "Transcripts"),
        data={
            "medium": "unify_message",
            "sender_id": 0,
            "receiver_ids": [1],
            "timestamp": datetime.now().astimezone().isoformat(),
            "content": "A normal assistant-authored chat row is not the opener.",
        },
    )
    dbsession.commit()

    first = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Welcome to your Coordinator."},
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )
    assert first.status_code == status.HTTP_409_CONFLICT, first.json()
    assert first.json()["detail"] == "coordinator_transcript_not_empty"

    transcripts = _context(
        dbsession,
        project=project,
        name=_assistant_context_name(coordinator, "Transcripts"),
    )
    logs = _context_logs(dbsession, context=transcripts)
    opener_logs = [
        log
        for log in logs
        if log.data.get("metadata", {}).get("source") == "coordinator_opener"
    ]
    assert not opener_logs
    _assert_owner_contact_row(
        dbsession,
        coordinator=coordinator,
        owner_user_id=owner["id"],
    )


@pytest.mark.anyio
async def test_transcript_seed_rejects_non_empty_exchange_history(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Transcript seeding also conflicts when exchange history already exists."""
    owner = await _create_user(client, "seed-exchange")
    org_data = await _create_org(client, owner, "seed-exchange")
    coordinator_id = int(org_data["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    project = _assistants_project(dbsession, coordinator=coordinator)
    _insert_log(
        dbsession,
        project=project,
        context_name=_assistant_context_name(coordinator, "Exchanges"),
        data={"medium": "unify_message"},
    )
    dbsession.commit()

    response = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Welcome to your Coordinator."},
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )

    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"] == "coordinator_transcript_not_empty"


@pytest.mark.anyio
async def test_assistant_list_tolerates_missing_coordinator_owner_contact_row(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Assistant reads do not repair historical Coordinators missing chat contact rows."""
    owner = await _create_user(client, "contact-repair")
    org_data = await _create_org(client, owner, "contact-repair")
    coordinator_id = int(org_data["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    project = _assistants_project(dbsession, coordinator=coordinator)
    contacts = _context(
        dbsession,
        project=project,
        name=_assistant_context_name(coordinator, "Contacts"),
    )
    assert contacts is not None
    dbsession.delete(contacts)
    dbsession.commit()

    response = await client.get(
        f"/v0/assistant?agent_id={coordinator_id}",
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["info"][0]["self_contact_id"] == 0
    assert response.json()["info"][0]["boss_contact_id"] == 1
    assert (
        _context(
            dbsession,
            project=project,
            name=_assistant_context_name(coordinator, "Contacts"),
        )
        is None
    )


@pytest.mark.anyio
async def test_coordinator_opt_in_repairs_missing_owner_contact_row(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Explicit Coordinator provisioning repairs missing owner contact rows."""
    owner = await _create_user(client, "contact-repair")
    org_data = await _create_org(client, owner, "contact-repair")
    coordinator_id = int(org_data["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    project = _assistants_project(dbsession, coordinator=coordinator)
    contacts = _context(
        dbsession,
        project=project,
        name=_assistant_context_name(coordinator, "Contacts"),
    )
    assert contacts is not None
    dbsession.delete(contacts)
    dbsession.commit()

    response = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        params={"organization_id": org_data["id"]},
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    _assert_owner_contact_row(
        dbsession,
        coordinator=coordinator,
        owner_user_id=owner["id"],
    )


@pytest.mark.anyio
async def test_org_assistant_creation_bootstraps_owner_contact_row(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Org-scoped POST /assistant writes the owner contact row for chat lookup."""
    credits_response = await client.get("/v0/credits", headers=HEADERS)
    assert credits_response.status_code == status.HTTP_200_OK, credits_response.json()
    owner = {"id": credits_response.json()["id"], "headers": HEADERS}
    org_data = await _create_org(client, owner, "org-assistant-owner-contact")
    headers = {"Authorization": f"Bearer {org_data['api_key']}"}
    organization = dbsession.get(Organization, org_data["id"])
    assert organization is not None
    billing_account = BillingAccount(
        credits=10_000,
        billing_setup_complete=True,
    )
    dbsession.add(billing_account)
    dbsession.flush()
    organization.billing_account_id = billing_account.id
    dbsession.commit()

    create = await client.post(
        "/v0/assistant",
        json={
            "first_name": "Org",
            "surname": "Bootstrap",
            "create_infra": False,
        },
        headers=headers,
    )
    assert create.status_code == status.HTTP_200_OK, create.json()
    assistant_id = int(create.json()["info"]["agent_id"])
    assistant = dbsession.get(Assistant, assistant_id)
    assert assistant is not None
    assert assistant.organization_id == org_data["id"]
    assert assistant.is_coordinator is False

    _assert_owner_contact_row(
        dbsession,
        coordinator=assistant,
        owner_user_id=owner["id"],
    )


@pytest.mark.anyio
async def test_reset_clears_only_coordinator_contexts(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Reset clears the Coordinator's private state without touching credentials."""
    owner = await _create_user(client, "state-reset")
    org_data = await _create_org(client, owner, "state-reset")
    coordinator_id = int(org_data["coordinator_id"])
    headers = {"Authorization": f"Bearer {org_data['api_key']}"}

    coordinator = dbsession.get(Assistant, coordinator_id)
    project = _assistants_project(dbsession, coordinator=coordinator)
    for suffix, data in (
        ("Coordinator/State", {"onboarding_active": False}),
        ("Coordinator/Checklist", {}),
        ("Transcripts", {"role": "assistant", "content": "Welcome."}),
        ("Exchanges", {"value": "exchange"}),
    ):
        _insert_log(
            dbsession,
            project=project,
            context_name=_assistant_context_name(coordinator, suffix),
            data=data,
        )
    dbsession.add(
        AssistantSecret(
            user_id=owner["id"],
            agent_id=coordinator_id,
            secret_name="OAUTH_TOKEN",
            secret_value="token",
        ),
    )
    dbsession.flush()

    reset = await client.post(
        f"/v0/assistant/{coordinator_id}/reset",
        headers=headers,
    )
    assert reset.status_code == status.HTTP_200_OK, reset.json()
    assert reset.json()["info"]["coordinator_id"] == str(coordinator_id)

    for suffix in (
        "Coordinator/State",
        "Coordinator/Checklist",
        "Transcripts",
        "Exchanges",
    ):
        assert (
            _context(
                dbsession,
                project=project,
                name=_assistant_context_name(coordinator, suffix),
            )
            is None
        )
    assert dbsession.get(AssistantSecret, (coordinator_id, "OAUTH_TOKEN")) is not None

    second_reset = await client.post(
        f"/v0/assistant/{coordinator_id}/reset",
        headers=headers,
    )
    assert second_reset.status_code == status.HTTP_200_OK, second_reset.json()


@pytest.mark.anyio
async def test_coordinator_provisioning_seeds_initial_state_row(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Newly-provisioned Coordinators start with onboarding active."""
    owner = await _create_user(client, "state-seed-personal")

    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    response = await client.get(
        f"/v0/assistant/{coordinator_id}/state",
        headers=owner["headers"],
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    payload = response.json()["info"]
    assert payload["coordinator_id"] == coordinator_id
    assert payload["onboarding_active"] is True
    assert payload["onboarding_step"] is None
    assert payload["started_at"] is not None
    assert payload["ended_at"] is None
    assert payload["intro_watched"] is False
    # A fresh Coordinator has completed nothing — notably the
    # platform-provisioned universal Unity email contact must NOT
    # count as a connected workspace.
    assert payload["completed_step_ids"] == []


def _render_step_ids(render: dict) -> set[str]:
    return {step["id"] for step in render["steps"]}


def _render_step(render: dict, step_id: str) -> dict:
    return next(step for step in render["steps"] if step["id"] == step_id)


@pytest.mark.anyio
async def test_onboarding_render_gates_teams_and_specialises_copy_by_provider(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Provider-exclusive steps + provider-aware copy follow the connected workspace.

    The Microsoft-only Teams demo renders only once a Microsoft workspace is
    connected, is hidden for Google (and before any connection), and the shared
    files demo's description specialises to the connected provider.
    """
    owner = await _create_user(client, "provider-gated-onboarding")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    dao = svc.AssistantSecretDAO(dbsession)

    # No workspace connected: Teams is hidden, files copy stays neutral, and
    # the provider-agnostic catalog never lists a provider-exclusive step.
    render = svc.compute_onboarding_render(dbsession, coordinator=coordinator)
    assert "workspace-teams" not in _render_step_ids(render)
    assert "Drive or OneDrive" in _render_step(render, "workspace-drive")["description"]
    catalog = svc.build_onboarding_catalog()
    assert "workspace-teams" not in {step["id"] for step in catalog["steps"]}

    # Google workspace: still no Teams, and the files copy names Google Drive.
    dao.upsert(
        coordinator.user_id,
        coordinator.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    render = svc.compute_onboarding_render(dbsession, coordinator=coordinator)
    assert "workspace-teams" not in _render_step_ids(render)
    assert "Google Drive" in _render_step(render, "workspace-drive")["description"]

    # Microsoft workspace: Teams surfaces as an available demo (workspace is
    # connected), and the files copy names OneDrive/SharePoint.
    dao.delete(coordinator.agent_id, "GOOGLE_GRANTED_SCOPES")
    dao.upsert(
        coordinator.user_id,
        coordinator.agent_id,
        "MICROSOFT_GRANTED_SCOPES",
        "Files.Read.All ChannelMessage.Read.All",
    )
    render = svc.compute_onboarding_render(dbsession, coordinator=coordinator)
    assert "workspace-teams" in _render_step_ids(render)
    assert _render_step(render, "workspace-teams")["status"] == "available"
    assert "workspace-teams" in {t["id"] for t in render["next_targets"]}
    assert "OneDrive" in _render_step(render, "workspace-drive")["description"]


@pytest.mark.anyio
async def test_assistant_read_workspace_provider_tracks_oauth_grant_not_mailbox(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """``workspace_provider`` mirrors the OAuth grant, not the mailbox tenant.

    A Coordinator keeps a platform Google mailbox (``email_provider``) while its
    connected workspace is whatever the owner OAuth-linked. The profile card
    reads ``workspace_provider``, so it must follow the granted-scopes secret
    (Google-first precedence) and never conflate it with the mailbox provider.
    """
    owner = await _create_user(client, "workspace-provider-tracks-grant")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    dao = svc.AssistantSecretDAO(dbsession)

    async def _read_coordinator() -> dict:
        resp = await client.get("/v0/assistant?demo=true", headers=owner["headers"])
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        return next(
            a for a in resp.json()["info"] if str(a["agent_id"]) == str(coordinator_id)
        )

    # No OAuth grant yet: no connected workspace, regardless of mailbox tenant.
    entry = await _read_coordinator()
    assert entry["workspace_provider"] is None
    mailbox_provider = entry["email_provider"]

    # Microsoft connected; the mailbox tenant is unchanged.
    dao.upsert(
        coordinator.user_id,
        coordinator.agent_id,
        "MICROSOFT_GRANTED_SCOPES",
        "Files.Read.All ChannelMessage.Read.All",
    )
    dbsession.commit()
    entry = await _read_coordinator()
    assert entry["workspace_provider"] == "microsoft"
    assert entry["email_provider"] == mailbox_provider

    # Switching to a Google grant flips the connected provider.
    dao.delete(coordinator.agent_id, "MICROSOFT_GRANTED_SCOPES")
    dao.upsert(
        coordinator.user_id,
        coordinator.agent_id,
        "GOOGLE_GRANTED_SCOPES",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    dbsession.commit()
    entry = await _read_coordinator()
    assert entry["workspace_provider"] == "google"

    # Disconnecting all grants clears the connected-workspace provider.
    dao.delete(coordinator.agent_id, "GOOGLE_GRANTED_SCOPES")
    dbsession.commit()
    entry = await _read_coordinator()
    assert entry["workspace_provider"] is None


@pytest.mark.anyio
async def test_assistant_list_does_not_bootstrap_coordinator_owner_contact_row(
    client: AsyncClient,
) -> None:
    """Assistant list reads must not acquire owner-contact write locks."""
    owner = await _create_user(client, "list-read-only-owner-row")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()

    with patch(
        "orchestra.web.api.assistant.views.ensure_owner_contact_row",
        side_effect=AssertionError("list must not bootstrap owner contacts"),
    ):
        response = await client.get("/v0/assistant?demo=true", headers=owner["headers"])

    assert response.status_code == status.HTTP_200_OK, response.json()


@pytest.mark.anyio
async def test_workspace_coordinator_backfill_marks_existing_user_intro_watched(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Admin backfill creates existing-user Coordinators with intro_watched set."""
    owner = await _create_user(client, "backfill-intro-watched")
    existing = dbsession.scalar(
        select(Assistant).where(
            Assistant.user_id == owner["id"],
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        ),
    )
    assert existing is not None
    dbsession.delete(existing)
    dbsession.commit()

    response = await client.post(
        "/v0/admin/coordinator/workspace/backfill",
        params={"dry_run": "false", "limit": 5000},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    coordinator = dbsession.scalar(
        select(Assistant).where(
            Assistant.user_id == owner["id"],
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        ),
    )
    assert coordinator is not None

    state = await client.get(
        f"/v0/assistant/{coordinator.agent_id}/state",
        headers=owner["headers"],
    )
    assert state.status_code == status.HTTP_200_OK, state.json()
    assert state.json()["info"]["onboarding_active"] is True
    assert state.json()["info"]["intro_watched"] is True


@pytest.mark.anyio
async def test_local_test_user_coordinator_helper_provisions_bare_user(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local shell bootstrap repairs raw test users with a Coordinator."""
    monkeypatch.delenv("SELF_HOST", raising=False)
    user = UserDAO(dbsession).create(
        email="local-test-user-coordinator@test.com",
        name="Local",
    )
    dbsession.commit()

    coordinator_id, created = await ensure_test_user_coordinator(
        dbsession,
        str(user.id),
    )

    assert created is True
    assert os.environ.get("SELF_HOST") is None
    coordinator = dbsession.scalar(
        select(Assistant).where(
            Assistant.agent_id == coordinator_id,
            Assistant.user_id == str(user.id),
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        ),
    )
    assert coordinator is not None
    assert coordinator.first_name == COORDINATOR_DEFAULT_FIRST_NAME


@pytest.mark.anyio
async def test_intro_watched_backfill_marks_existing_coordinator_state(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Existing Coordinator state is marked watched without changing onboarding_active."""
    owner = await _create_user(client, "intro-state-backfill")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    initial = await client.get(
        f"/v0/assistant/{coordinator_id}/state",
        headers=owner["headers"],
    )
    assert initial.status_code == status.HTTP_200_OK, initial.json()
    assert initial.json()["info"]["intro_watched"] is False

    response = await client.post(
        "/v0/admin/coordinator/intro-watched/backfill",
        params={"dry_run": "false", "limit": 5000},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    follow_up = await client.get(
        f"/v0/assistant/{coordinator_id}/state",
        headers=owner["headers"],
    )
    assert follow_up.status_code == status.HTTP_200_OK, follow_up.json()
    assert follow_up.json()["info"]["onboarding_active"] is True
    assert follow_up.json()["info"]["intro_watched"] is True


@pytest.mark.anyio
async def test_coordinator_state_seed_is_idempotent_on_repair(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Repeated opt-in calls do not duplicate the ``Coordinator/State`` row."""
    owner = await _create_user(client, "state-seed-idempotent")

    first = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert first.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, first.json()
    coordinator_id = int(first.json()["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    assert coordinator is not None

    second = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert second.status_code == status.HTTP_200_OK, second.json()

    project = _assistants_project(dbsession, coordinator=coordinator)
    state_context = _context(
        dbsession,
        project=project,
        name=_assistant_context_name(coordinator, "Coordinator/State"),
    )
    assert state_context is not None
    assert len(_context_logs(dbsession, context=state_context)) == 1


@pytest.mark.anyio
async def test_coordinator_state_patch_records_onboarding_step(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Recording an onboarding step persists on the row for resumption."""
    owner = await _create_user(client, "state-step")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    with patch(
        "orchestra.web.api.assistant.views.emit_onboarding_step_started_event",
        new=AsyncMock(return_value=True),
    ) as emit:
        patch_response = await client.patch(
            f"/v0/assistant/{coordinator_id}/state",
            json={"onboarding_step": "email-reply"},
            headers=owner["headers"],
        )
    assert patch_response.status_code == status.HTTP_200_OK, patch_response.json()
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["step_id"] == "email-reply"
    info = patch_response.json()["info"]
    assert info["onboarding_active"] is True
    assert info["onboarding_step"] == "email-reply"

    follow_up = await client.get(
        f"/v0/assistant/{coordinator_id}/state",
        headers=owner["headers"],
    )
    assert follow_up.status_code == status.HTTP_200_OK, follow_up.json()
    assert follow_up.json()["info"]["onboarding_step"] == "email-reply"

    invalid = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_step": "briefing"},
        headers=owner["headers"],
    )
    assert invalid.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_coordinator_state_patch_reset_emits_reset_event(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Resetting a step emits the reset narration so the brain de-sticks it."""
    owner = await _create_user(client, "state-reset-step")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    with patch(
        "orchestra.web.api.assistant.views.emit_onboarding_step_reset_event",
        new=AsyncMock(return_value=True),
    ) as emit:
        reset = await client.patch(
            f"/v0/assistant/{coordinator_id}/state",
            json={"reset_onboarding_step": "workspace"},
            headers=owner["headers"],
        )
        assert reset.status_code == status.HTTP_200_OK, reset.json()
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["step_id"] == "workspace"


@pytest.mark.anyio
async def test_coordinator_state_intro_watched_is_one_way_sticky(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """``intro_watched`` latches ``True`` and survives later transitions.

    The console sets this once the user resolves the opening picker so
    the ringing picker / auto-playing intro never re-appear on a later
    page load. It must carry forward across unrelated PATCHes and must
    not be resettable to ``False``.
    """
    owner = await _create_user(client, "state-intro-watched")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    watched = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"intro_watched": True},
        headers=owner["headers"],
    )
    assert watched.status_code == status.HTTP_200_OK, watched.json()
    assert watched.json()["info"]["intro_watched"] is True

    # An unrelated PATCH carries the flag forward untouched.
    step = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_step": "email-reply"},
        headers=owner["headers"],
    )
    assert step.status_code == status.HTTP_200_OK, step.json()
    assert step.json()["info"]["intro_watched"] is True

    # Attempting to reset to False is ignored (one-way sticky).
    reset_attempt = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"intro_watched": False},
        headers=owner["headers"],
    )
    assert reset_attempt.status_code == status.HTTP_200_OK, reset_attempt.json()
    assert reset_attempt.json()["info"]["intro_watched"] is True

    follow_up = await client.get(
        f"/v0/assistant/{coordinator_id}/state",
        headers=owner["headers"],
    )
    assert follow_up.status_code == status.HTTP_200_OK, follow_up.json()
    assert follow_up.json()["info"]["intro_watched"] is True


@pytest.mark.anyio
async def test_coordinator_state_patch_deactivates_onboarding_and_stamps_ended_at(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Deactivating onboarding stamps ``ended_at`` exactly once."""
    owner = await _create_user(client, "state-working")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    promote = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": False, "clear_onboarding_step": True},
        headers=owner["headers"],
    )
    assert promote.status_code == status.HTTP_200_OK, promote.json()
    info = promote.json()["info"]
    assert info["onboarding_active"] is False
    assert info["onboarding_step"] is None
    assert info["started_at"] is not None
    first_ended_at = info["ended_at"]
    assert first_ended_at is not None

    # A no-op write should preserve ``ended_at`` rather than re-stamp it.
    noop = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": False},
        headers=owner["headers"],
    )
    assert noop.status_code == status.HTTP_200_OK, noop.json()
    assert noop.json()["info"]["ended_at"] == first_ended_at


@pytest.mark.anyio
async def test_coordinator_state_patch_onboarding_active_with_assistant_api_key(
    client: AsyncClient,
) -> None:
    """The coordinator runtime API key can toggle ``onboarding_active``."""
    owner = await _create_user(client, "state-runtime-key")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])
    admin = await client.get(
        f"/v0/admin/assistant?agent_id={coordinator_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin.status_code == status.HTTP_200_OK, admin.json()
    runtime_key = admin.json()["info"][0]["api_key"]
    runtime_headers = {"Authorization": f"Bearer {runtime_key}"}

    deactivate = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": False, "clear_onboarding_step": True},
        headers=runtime_headers,
    )
    assert deactivate.status_code == status.HTTP_200_OK, deactivate.json()
    assert deactivate.json()["info"]["onboarding_active"] is False

    activate = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": True},
        headers=runtime_headers,
    )
    assert activate.status_code == status.HTTP_200_OK, activate.json()
    assert activate.json()["info"]["onboarding_active"] is True


@pytest.mark.anyio
async def test_coordinator_state_patch_manual_step_completion(
    client: AsyncClient,
) -> None:
    """The slow brain can manually complete settable onboarding steps."""
    owner = await _create_user(client, "state-manual-complete")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    complete = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={
            "onboarding_step_completion": {
                "step_id": "create-scheduled-task",
                "completed": True,
            },
        },
        headers=owner["headers"],
    )
    assert complete.status_code == status.HTTP_200_OK, complete.json()
    info = complete.json()["info"]
    assert "create-scheduled-task" in info["completed_step_ids"]
    steps = {step["id"]: step for step in info["onboarding"]["steps"]}
    assert steps["create-scheduled-task"]["status"] == "done"
    assert steps["create-scheduled-task"]["manually_completed"] is True

    uncomplete = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={
            "onboarding_step_completion": {
                "step_id": "create-scheduled-task",
                "completed": False,
            },
        },
        headers=owner["headers"],
    )
    assert uncomplete.status_code == status.HTTP_200_OK, uncomplete.json()
    info = uncomplete.json()["info"]
    assert "create-scheduled-task" not in info["completed_step_ids"]
    assert info["onboarding"]["steps"]
    steps = {step["id"]: step for step in info["onboarding"]["steps"]}
    assert steps["create-scheduled-task"]["status"] != "done"


@pytest.mark.anyio
async def test_coordinator_state_patch_manual_step_completion_rejects_auto_steps(
    client: AsyncClient,
) -> None:
    """Auto-triggered Communication rows return an explanatory 400."""
    owner = await _create_user(client, "state-manual-reject")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    blocked = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={
            "onboarding_step_completion": {
                "step_id": "email-reference",
                "completed": True,
            },
        },
        headers=owner["headers"],
    )
    assert blocked.status_code == status.HTTP_400_BAD_REQUEST, blocked.json()
    detail = blocked.json()["detail"]
    assert detail["code"] == "onboarding_step_not_manually_settable"
    assert "Communication" in detail["message"]


@pytest.mark.anyio
async def test_coordinator_state_patch_manual_step_completion_with_assistant_api_key(
    client: AsyncClient,
) -> None:
    """The coordinator runtime API key can toggle manual step completion."""
    owner = await _create_user(client, "state-manual-runtime-key")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])
    admin = await client.get(
        f"/v0/admin/assistant?agent_id={coordinator_id}",
        headers=ADMIN_HEADERS,
    )
    assert admin.status_code == status.HTTP_200_OK, admin.json()
    runtime_key = admin.json()["info"][0]["api_key"]
    runtime_headers = {"Authorization": f"Bearer {runtime_key}"}

    complete = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={
            "onboarding_step_completion": {
                "step_id": "apps",
                "completed": True,
            },
        },
        headers=runtime_headers,
    )
    assert complete.status_code == status.HTTP_200_OK, complete.json()
    assert "apps" in complete.json()["info"]["completed_step_ids"]


@pytest.mark.anyio
async def test_coordinator_state_patch_reset_clears_manual_completion(
    client: AsyncClient,
) -> None:
    """Resetting a step clears any manual completion flag for it."""
    owner = await _create_user(client, "state-manual-reset")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={
            "onboarding_step_completion": {
                "step_id": "create-scheduled-task",
                "completed": True,
            },
        },
        headers=owner["headers"],
    )
    reset = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"reset_onboarding_step": "create-scheduled-task"},
        headers=owner["headers"],
    )
    assert reset.status_code == status.HTTP_200_OK, reset.json()
    info = reset.json()["info"]
    assert "create-scheduled-task" not in info["completed_step_ids"]
    steps = {step["id"]: step for step in info["onboarding"]["steps"]}
    assert steps["create-scheduled-task"]["manually_completed"] is False


@pytest.mark.anyio
async def test_coordinator_state_patch_resume_clears_ended_at(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Resuming onboarding clears ``ended_at``.

    A row with ``onboarding_active=True`` and a stamped ``ended_at`` is
    semantically incoherent. The resume path must wipe the timestamp; a
    subsequent deactivation re-stamps it from scratch.
    """
    owner = await _create_user(client, "state-resume")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    # Skip → working, ``ended_at`` is stamped.
    skip = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": False, "clear_onboarding_step": True},
        headers=owner["headers"],
    )
    assert skip.status_code == status.HTTP_200_OK, skip.json()
    first_ended_at = skip.json()["info"]["ended_at"]
    assert first_ended_at is not None

    # Resume → onboarding clears it.
    resume = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": True},
        headers=owner["headers"],
    )
    assert resume.status_code == status.HTTP_200_OK, resume.json()
    resumed = resume.json()["info"]
    assert resumed["onboarding_active"] is True
    assert resumed["ended_at"] is None
    # ``started_at`` is sticky across the round-trip so we still
    # know when the lifecycle began.
    assert resumed["started_at"] is not None

    # Re-skipping re-stamps a fresh ``ended_at`` (and it must be
    # strictly after the first one, since the row clears in
    # between).
    re_skip = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": False, "clear_onboarding_step": True},
        headers=owner["headers"],
    )
    assert re_skip.status_code == status.HTTP_200_OK, re_skip.json()
    second_ended_at = re_skip.json()["info"]["ended_at"]
    assert second_ended_at is not None
    assert second_ended_at >= first_ended_at


@pytest.mark.anyio
async def test_coordinator_state_patch_rejects_invalid_values(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Empty step strings and unknown skip ids fail validation up front."""
    owner = await _create_user(client, "state-invalid")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    empty_step = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_step": ""},
        headers=owner["headers"],
    )
    assert empty_step.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    unknown_skip = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"skip_onboarding_step": "not-a-step"},
        headers=owner["headers"],
    )
    assert unknown_skip.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_coordinator_state_forbidden_for_non_owner(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Only the owning user can read or update Coordinator/State."""
    owner = await _create_user(client, "state-owner")
    intruder = await _create_user(client, "state-intruder")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    read = await client.get(
        f"/v0/assistant/{coordinator_id}/state",
        headers=intruder["headers"],
    )
    assert read.status_code == status.HTTP_403_FORBIDDEN

    write = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_active": False},
        headers=intruder["headers"],
    )
    assert write.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_personal_opt_in_repairs_unset_desktop_mode(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Personal opt-in restores the Coordinator desktop default when missing."""
    owner = await _create_user(client, "personal-desktop-default")

    first = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert first.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, first.json()
    coordinator_id = int(first.json()["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    assert coordinator is not None
    assert coordinator.desktop_mode == EXPECTED_COORDINATOR_DEFAULT_DESKTOP_MODE

    coordinator.desktop_mode = None
    dbsession.commit()

    repaired = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert repaired.status_code == status.HTTP_200_OK, repaired.json()
    assert repaired.json()["coordinator_id"] == str(coordinator_id)

    dbsession.refresh(coordinator)
    assert coordinator.desktop_mode == EXPECTED_COORDINATOR_DEFAULT_DESKTOP_MODE


@pytest.mark.anyio
async def test_personal_opt_in_repairs_defaults_and_generic_surfaces_reject_flag(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Personal Coordinator opt-in repairs defaults and keeps generic writes closed."""
    owner = await _create_user(client, "personal")
    other = await _create_user(client, "personal-other")

    first = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert first.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, first.json()
    coordinator_id = first.json()["coordinator_id"]
    coordinator = dbsession.get(Assistant, int(coordinator_id))
    assert coordinator is not None
    assert coordinator.nationality == EXPECTED_COORDINATOR_DEFAULT_NATIONALITY
    assert coordinator.desktop_mode == EXPECTED_COORDINATOR_DEFAULT_DESKTOP_MODE
    assert coordinator.about == ""
    assert coordinator.first_name == COORDINATOR_DEFAULT_FIRST_NAME
    assert coordinator.job_title == COORDINATOR_DEFAULT_JOB_TITLE
    coordinator.nationality = None
    coordinator.desktop_mode = None
    dbsession.commit()

    second = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert second.status_code == status.HTTP_200_OK, second.json()
    assert second.json()["coordinator_id"] == coordinator_id
    dbsession.refresh(coordinator)
    assert coordinator.nationality == EXPECTED_COORDINATOR_DEFAULT_NATIONALITY
    assert coordinator.desktop_mode == EXPECTED_COORDINATOR_DEFAULT_DESKTOP_MODE
    assert coordinator.about == ""
    assert coordinator.first_name == COORDINATOR_DEFAULT_FIRST_NAME
    assert coordinator.job_title == COORDINATOR_DEFAULT_JOB_TITLE

    dbsession.execute(
        delete(ContactMembership).where(
            ContactMembership.assistant_id == int(coordinator_id),
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
            ContactMembership.relationship == CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
        ),
    )
    dbsession.commit()

    repaired = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert repaired.status_code == status.HTTP_200_OK, repaired.json()
    assert repaired.json()["coordinator_id"] == coordinator_id
    assert {
        (membership.contact_id, membership.relationship)
        for membership in _personal_memberships(
            dbsession,
            assistant_id=int(coordinator_id),
        )
    } == {
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    }
    _assert_owner_contact_row(
        dbsession,
        coordinator=coordinator,
        owner_user_id=owner["id"],
    )

    mismatch = await client.post(
        f"/v0/user/{other['id']}/coordinator",
        headers=owner["headers"],
    )
    assert mismatch.status_code == status.HTTP_403_FORBIDDEN

    create = await client.post(
        "/v0/assistant",
        json={"first_name": "Nope", "is_coordinator": True, "is_local": True},
        headers=HEADERS,
    )
    assert create.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    update = await client.patch(
        f"/v0/assistant/{coordinator_id}/config",
        json={"is_coordinator": False},
        headers=owner["headers"],
    )
    assert update.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_personal_opt_in_repairs_legacy_numeric_owner_contact_id(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Personal coordinator opt-in repairs legacy numeric owner-contact ids."""
    owner = await _create_user(client, "personal-stale-contact")
    first = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert first.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, first.json()
    coordinator_id = int(first.json()["coordinator_id"])
    coordinator = dbsession.get(Assistant, coordinator_id)
    assert coordinator is not None
    project = _assistants_project(dbsession, coordinator=coordinator)
    contacts = _context(
        dbsession,
        project=project,
        name=_assistant_context_name(coordinator, "Contacts"),
    )
    assert contacts is not None
    owner_log = next(
        (
            log
            for log in _context_logs(dbsession, context=contacts)
            if log.data.get("contact_id") == 1
        ),
        None,
    )
    assert owner_log is not None

    # Simulate historical corruption: numeric equivalent stored as float.
    owner_log.data = {**owner_log.data, "contact_id": 1.0}
    flag_modified(owner_log, "data")
    dbsession.commit()

    repaired = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert repaired.status_code == status.HTTP_200_OK, repaired.json()
    assert repaired.json()["coordinator_id"] == str(coordinator_id)

    refreshed_owner_log = _assert_owner_contact_row(
        dbsession,
        coordinator=coordinator,
        owner_user_id=owner["id"],
    )
    assert refreshed_owner_log.id == owner_log.id


@pytest.mark.anyio
async def test_personal_coordinator_requires_owner_for_lifecycle_operations(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Only the owner can mutate a personal coordinator; delete stays guarded."""
    owner = await _create_user(client, "admin-gate-owner")
    member = await _create_user(client, "admin-gate-member")
    admin = await _create_user(client, "admin-gate-admin")
    org_data = await _create_org(client, owner, "admin-gate")
    coordinator_id = int(org_data["coordinator_id"])

    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    org_member_dao = OrganizationMemberDAO(dbsession)
    org_member_dao.create(
        organization_id=org_data["id"],
        user_id=member["id"],
        role_id=member_role.id,
    )
    org_member_dao.create(
        organization_id=org_data["id"],
        user_id=admin["id"],
        role_id=admin_role.id,
    )
    dbsession.flush()

    member_seed = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Member cannot seed."},
        headers=member["headers"],
    )
    assert member_seed.status_code == status.HTTP_403_FORBIDDEN, member_seed.json()

    member_reset = await client.post(
        f"/v0/assistant/{coordinator_id}/reset",
        headers=member["headers"],
    )
    assert member_reset.status_code == status.HTTP_403_FORBIDDEN, member_reset.json()

    admin_seed = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Admin cannot seed."},
        headers=admin["headers"],
    )
    assert admin_seed.status_code == status.HTTP_403_FORBIDDEN, admin_seed.json()

    admin_reset = await client.post(
        f"/v0/assistant/{coordinator_id}/reset",
        headers=admin["headers"],
    )
    assert admin_reset.status_code == status.HTTP_403_FORBIDDEN, admin_reset.json()

    owner_seed = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Owner can seed."},
        headers=owner["headers"],
    )
    assert owner_seed.status_code == status.HTTP_200_OK, owner_seed.json()

    owner_reset = await client.post(
        f"/v0/assistant/{coordinator_id}/reset",
        headers=owner["headers"],
    )
    assert owner_reset.status_code == status.HTTP_200_OK, owner_reset.json()

    delete = await client.delete(
        f"/v0/assistant/{coordinator_id}",
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )
    assert delete.status_code == status.HTTP_409_CONFLICT, delete.json()
    assert delete.json()["detail"] == "cannot_delete_coordinator"
    assert dbsession.get(Assistant, coordinator_id) is not None


@pytest.mark.anyio
async def test_delegate_to_colleague_dispatches_without_target_owned_rows(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinator delegation dispatches a wake reason without direct row writes."""
    owner = await _create_user(client, "delegate-owner")
    org_data = await _create_org(client, owner, "delegate")
    target = Assistant(
        user_id=owner["id"],
        organization_id=org_data["id"],
        first_name="Revenue",
        surname="Ops",
    )
    dbsession.add(target)
    dbsession.flush()
    coordinator = dbsession.scalar(
        select(Assistant).where(
            Assistant.organization_id == org_data["id"],
            Assistant.is_coordinator.is_(True),
        ),
    )
    assert coordinator is not None
    delegate_runtime = AsyncMock(
        return_value={"status": "attached_to_startup", "activation_id": "act-1"},
    )
    monkeypatch.setattr(
        "orchestra.web.api.assistant.views.delegate_to_colleague_runtime",
        delegate_runtime,
    )

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/delegate",
        json={
            "instruction": "Schedule the renewal risk summary tomorrow morning.",
            "intent": "schedule_task",
            "dedupe_key": "renewal-risk-42",
            "related_context": {"source": "coordinator"},
        },
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    payload = response.json()["info"]
    assert payload == {
        "coordinator_id": coordinator.agent_id,
        "target_assistant_id": target.agent_id,
        "status": "attached_to_startup",
        "activation_id": "act-1",
        "accepted": True,
        "completion_status": "pending_async",
        "receipt_type": "async_delegation_receipt",
        "message": (
            "The colleague has been woken or notified with the assignment. "
            "This does not mean the colleague has already created durable artifacts "
            "or completed the work."
        ),
    }
    delegate_runtime.assert_awaited_once_with(
        assistant_id=target.agent_id,
        requested_by_assistant_id=coordinator.agent_id,
        instruction="Schedule the renewal risk summary tomorrow morning.",
        intent="schedule_task",
        dedupe_key="renewal-risk-42",
        related_context={"source": "coordinator"},
    )
    leaked_contexts = dbsession.scalars(
        select(Context).where(
            Context.name.in_(
                [
                    _assistant_context_name(target, "Tasks"),
                    _assistant_context_name(target, "Knowledge"),
                ],
            ),
        ),
    ).all()
    assert leaked_contexts == []


@pytest.mark.anyio
async def test_delegate_rejects_blank_instruction_before_dispatch(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delegation requires a meaningful assignment."""
    owner = await _create_user(client, "delegate-blank")
    org_data = await _create_org(client, owner, "delegate-blank")
    target = Assistant(
        user_id=owner["id"],
        organization_id=org_data["id"],
        first_name="Support",
        surname="Ops",
    )
    dbsession.add(target)
    dbsession.flush()
    delegate_runtime = AsyncMock()
    monkeypatch.setattr(
        "orchestra.web.api.assistant.views.delegate_to_colleague_runtime",
        delegate_runtime,
    )

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/delegate",
        json={"instruction": "  ", "intent": "add_knowledge"},
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.json()
    delegate_runtime.assert_not_awaited()


@pytest.mark.anyio
async def test_delegate_requires_the_target_scope_coordinator(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Personal Coordinators cannot delegate to another user's colleague."""
    owner = await _create_user(client, "delegate-personal-owner")
    other = await _create_user(client, "delegate-personal-other")
    coordinator_response = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert coordinator_response.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, coordinator_response.json()
    target = Assistant(
        user_id=other["id"],
        first_name="Private",
        surname="Assistant",
    )
    dbsession.add(target)
    dbsession.flush()

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/delegate",
        json={"instruction": "Remember that renewal blockers come first."},
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.json()


@pytest.mark.anyio
async def test_delegate_org_target_resolves_authorized_coordinator(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Org delegation resolves an authorized Coordinator after workspace checks."""
    owner = await _create_user(client, "delegate-org-owner")
    member = await _create_user(client, "delegate-org-member")
    org_data = await _create_org(client, owner, "delegate-org-missing-personal")

    add_member = await client.post(
        f"/v0/organizations/{org_data['id']}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()

    target = Assistant(
        user_id=member["id"],
        organization_id=org_data["id"],
        first_name="Ops",
        surname="Target",
    )
    dbsession.add(target)
    dbsession.flush()

    member_coordinator = dbsession.scalar(
        select(Assistant).where(
            Assistant.user_id == member["id"],
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        ),
    )
    assert member_coordinator is not None
    dbsession.delete(member_coordinator)
    dbsession.commit()
    delegate_runtime = AsyncMock(return_value={"status": "published_to_active_session"})
    monkeypatch.setattr(
        "orchestra.web.api.assistant.views.delegate_to_colleague_runtime",
        delegate_runtime,
    )

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/delegate",
        json={"instruction": "Remember that renewal blockers come first."},
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    coordinator_id = response.json()["info"]["coordinator_id"]
    delegate_runtime.assert_awaited_once()
    assert (
        delegate_runtime.await_args.kwargs["requested_by_assistant_id"]
        == coordinator_id
    )


@pytest.mark.anyio
async def test_delegate_org_target_requires_org_write_access(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Users outside the org cannot delegate to org assistants."""
    owner = await _create_user(client, "delegate-org-rbac-owner")
    outsider = await _create_user(client, "delegate-org-rbac-outsider")
    org_data = await _create_org(client, owner, "delegate-org-rbac")

    target = Assistant(
        user_id=owner["id"],
        organization_id=org_data["id"],
        first_name="Finance",
        surname="Target",
    )
    dbsession.add(target)
    dbsession.commit()

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/delegate",
        json={"instruction": "Remember that renewal blockers come first."},
        headers=outsider["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.json()


@pytest.mark.anyio
async def test_onboarding_step_event_emits_task_chip_event(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """A Tasks-phase chip click publishes its canonical task_chip event."""
    owner = await _create_user(client, "step-event-chip")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    # ``_post_unity_system_event`` is the outbound Adapters HTTP hop — the one
    # external boundary with no local service. Everything upstream (view,
    # service gate, graph chip resolution) runs for real.
    with patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post:
        response = await client.post(
            f"/v0/assistant/{coordinator_id}/onboarding-step-event",
            json={"step_id": "create-scheduled-task", "chip_id": "inbox-sweep-soon"},
            headers=owner["headers"],
        )

    assert response.status_code == status.HTTP_200_OK, response.json()
    info = response.json()["info"]
    assert info["emitted"] is True
    assert info["chip_id"] == "inbox-sweep-soon"
    post.assert_awaited_once()
    extra = post.await_args.kwargs["extra_event_fields"]
    assert extra["subtype"] == "task_chip_requested"
    assert extra["details"]["step_id"] == "create-scheduled-task"
    assert (
        extra["details"]["instruction"]
        == "In two minutes, check my inbox and text me anything urgent"
    )


@pytest.mark.anyio
async def test_onboarding_step_event_unknown_chip_does_not_emit(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """An unknown chip id resolves to nothing, so no event is published."""
    owner = await _create_user(client, "step-event-bad-chip")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    with patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post:
        response = await client.post(
            f"/v0/assistant/{coordinator_id}/onboarding-step-event",
            json={"step_id": "create-scheduled-task", "chip_id": "no-such-chip"},
            headers=owner["headers"],
        )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["info"]["emitted"] is False
    post.assert_not_awaited()


@pytest.mark.anyio
async def test_onboarding_step_event_emits_task_beat_row_event(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """A bare beat-row click (no chip) publishes the freeform task_beat event."""
    owner = await _create_user(client, "step-event-beat-row")
    create = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert create.status_code in {
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    }, create.json()
    coordinator_id = int(create.json()["coordinator_id"])

    with patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post:
        response = await client.post(
            f"/v0/assistant/{coordinator_id}/onboarding-step-event",
            json={"step_id": "create-triggerable-task"},
            headers=owner["headers"],
        )

    assert response.status_code == status.HTTP_200_OK, response.json()
    info = response.json()["info"]
    assert info["emitted"] is True
    assert info["chip_id"] is None
    post.assert_awaited_once()
    extra = post.await_args.kwargs["extra_event_fields"]
    assert extra["subtype"] == "task_beat_requested"
    assert extra["details"]["task_kind"] == "triggered"
