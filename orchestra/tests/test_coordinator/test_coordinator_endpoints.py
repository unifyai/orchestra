"""Endpoint tests for Coordinator provisioning and lifecycle contracts."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
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
from orchestra.services.coordinator_personas import COORDINATOR_BIO
from orchestra.services.task_machine_state_service import (
    build_task_activation_context_name,
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
        "orchestra.db.dao.log_event_dao.BucketService",
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
) -> None:
    context = _context(dbsession, project=project, name=context_name)
    if context is None:
        context = Context(project_id=project.id, name=context_name)
        dbsession.add(context)
        dbsession.flush()
    log_event = LogEvent(project_id=project.id, data=data)
    dbsession.add(log_event)
    dbsession.flush()
    dbsession.add(
        LogEventContext(log_event_id=log_event.id, context_id=context.id),
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
    assert coordinator.organization_id is None
    assert coordinator.user_id == owner_user_id
    assert coordinator.nationality == EXPECTED_COORDINATOR_DEFAULT_NATIONALITY
    assert coordinator.desktop_mode == EXPECTED_COORDINATOR_DEFAULT_DESKTOP_MODE
    assert coordinator.about == COORDINATOR_BIO
    org_scoped_coordinator = dbsession.scalar(
        select(Assistant).where(
            Assistant.organization_id == org_data["id"],
            Assistant.is_coordinator.is_(True),
        ),
    )
    assert org_scoped_coordinator is None
    assert {
        (membership.contact_id, membership.relationship)
        for membership in _personal_memberships(
            dbsession,
            assistant_id=coordinator.agent_id,
        )
    } == {
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    }

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
async def test_assistant_list_repairs_missing_coordinator_owner_contact_row(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Assistant reads repair historical Coordinators missing chat contact rows."""
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
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["info"][0]["self_contact_id"] == 0
    assert response.json()["info"][0]["boss_contact_id"] == 1
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
        ("Coordinator/State", {"mode": "working"}),
        ("Coordinator/Checklist", {"title": "Connect HubSpot", "mode": "ready_to_go"}),
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
    """Newly-provisioned Coordinators land in ``onboarding`` mode."""
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
    assert payload["mode"] == "onboarding"
    assert payload["onboarding_step"] is None
    assert payload["started_at"] is not None
    assert payload["ended_at"] is None


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

    patch = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_step": "briefing"},
        headers=owner["headers"],
    )
    assert patch.status_code == status.HTTP_200_OK, patch.json()
    info = patch.json()["info"]
    assert info["mode"] == "onboarding"
    assert info["onboarding_step"] == "briefing"

    follow_up = await client.get(
        f"/v0/assistant/{coordinator_id}/state",
        headers=owner["headers"],
    )
    assert follow_up.status_code == status.HTTP_200_OK, follow_up.json()
    assert follow_up.json()["info"]["onboarding_step"] == "briefing"


@pytest.mark.anyio
async def test_coordinator_state_patch_promotes_to_working_and_stamps_ended_at(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Promoting to ``working`` stamps ``ended_at`` exactly once."""
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
        json={"mode": "working", "clear_onboarding_step": True},
        headers=owner["headers"],
    )
    assert promote.status_code == status.HTTP_200_OK, promote.json()
    info = promote.json()["info"]
    assert info["mode"] == "working"
    assert info["onboarding_step"] is None
    assert info["started_at"] is not None
    first_ended_at = info["ended_at"]
    assert first_ended_at is not None

    # A no-op write should preserve ``ended_at`` rather than re-stamp it.
    noop = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"mode": "working"},
        headers=owner["headers"],
    )
    assert noop.status_code == status.HTTP_200_OK, noop.json()
    assert noop.json()["info"]["ended_at"] == first_ended_at


@pytest.mark.anyio
async def test_coordinator_state_patch_resume_clears_ended_at(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Resuming onboarding (working → onboarding) clears ``ended_at``.

    A row in ``onboarding`` mode with a stamped ``ended_at`` is
    semantically incoherent ("onboarding finished on X, currently
    onboarding"). The resume path must wipe the timestamp; a
    subsequent skip / completion re-stamps it from scratch.
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
        json={"mode": "working", "clear_onboarding_step": True},
        headers=owner["headers"],
    )
    assert skip.status_code == status.HTTP_200_OK, skip.json()
    first_ended_at = skip.json()["info"]["ended_at"]
    assert first_ended_at is not None

    # Resume → onboarding clears it.
    resume = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"mode": "onboarding"},
        headers=owner["headers"],
    )
    assert resume.status_code == status.HTTP_200_OK, resume.json()
    resumed = resume.json()["info"]
    assert resumed["mode"] == "onboarding"
    assert resumed["ended_at"] is None
    # ``started_at`` is sticky across the round-trip so we still
    # know when the lifecycle began.
    assert resumed["started_at"] is not None

    # Re-skipping re-stamps a fresh ``ended_at`` (and it must be
    # strictly after the first one, since the row clears in
    # between).
    re_skip = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"mode": "working", "clear_onboarding_step": True},
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
    """Unknown modes and empty step strings fail validation up front."""
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

    # Checklist-mode vocabulary must NOT be accepted on Coordinator/State.
    bad_mode = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"mode": "ready_to_go"},
        headers=owner["headers"],
    )
    assert bad_mode.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    legacy_mode = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"mode": "active"},
        headers=owner["headers"],
    )
    assert legacy_mode.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    empty_step = await client.patch(
        f"/v0/assistant/{coordinator_id}/state",
        json={"onboarding_step": ""},
        headers=owner["headers"],
    )
    assert empty_step.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


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
        json={"mode": "working"},
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
    assert coordinator.about == COORDINATOR_BIO
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
    assert coordinator.about == COORDINATOR_BIO

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
        headers=owner["headers"],
    )
    assert delete.status_code == status.HTTP_409_CONFLICT, delete.json()
    assert delete.json()["detail"] == "cannot_delete_coordinator"
    assert dbsession.get(Assistant, coordinator_id) is not None


@pytest.mark.anyio
async def test_preseed_colleague_writes_target_owned_rows_and_task_activation(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Coordinator preseed writes rows under the target colleague root."""
    owner = await _create_user(client, "preseed-owner")
    org_data = await _create_org(client, owner, "preseed")
    coordinator_id = int(org_data["coordinator_id"])
    target = Assistant(
        user_id=owner["id"],
        organization_id=org_data["id"],
        first_name="Revenue",
        surname="Ops",
    )
    dbsession.add(target)
    dbsession.flush()
    tasks_context_name = _assistant_context_name(target, "Tasks")
    knowledge_context_name = _assistant_context_name(target, "Knowledge")

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/preseed",
        json={
            "writes": [
                {
                    "context": "Tasks",
                    "entries": [
                        {
                            "task_id": 701,
                            "instance_id": 0,
                            "status": "scheduled",
                            "name": "Morning renewal risk summary",
                            "schedule": {"start_at": "2026-05-07T08:00:00+00:00"},
                            "repeat": [{"unit": "day", "count": 1}],
                        },
                    ],
                },
                {
                    "context": "Knowledge",
                    "entries": [
                        {"topic": "Renewals", "content": "Check blockers first."},
                    ],
                },
            ],
        },
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    payload = response.json()["info"]
    assert payload["coordinator_id"] == coordinator_id
    assert payload["target_assistant_id"] == target.agent_id
    assert [write["context"] for write in payload["writes"]] == [
        tasks_context_name,
        knowledge_context_name,
    ]

    project = dbsession.scalar(
        select(Project).where(
            Project.organization_id == org_data["id"],
            Project.name == "Assistants",
        ),
    )
    assert project is not None
    tasks_context = _context(dbsession, project=project, name=tasks_context_name)
    knowledge_context = _context(
        dbsession,
        project=project,
        name=knowledge_context_name,
    )
    assert tasks_context is not None
    assert knowledge_context is not None

    task_rows = _context_logs(dbsession, context=tasks_context)
    assert len(task_rows) == 1
    task_data = task_rows[0].data
    assert task_data["authoring_assistant_id"] == coordinator_id
    assert task_data["_user_id"] == owner["id"]
    assert task_data["_assistant_id"] == str(target.agent_id)

    knowledge_rows = _context_logs(dbsession, context=knowledge_context)
    assert len(knowledge_rows) == 1
    assert knowledge_rows[0].data == {
        "topic": "Renewals",
        "content": "Check blockers first.",
        "authoring_assistant_id": coordinator_id,
    }

    activation_context = _context(
        dbsession,
        project=project,
        name=build_task_activation_context_name(tasks_context_name),
    )
    assert activation_context is not None
    activation_rows = _context_logs(dbsession, context=activation_context)
    assert len(activation_rows) == 1
    assert activation_rows[0].data["assistant_id"] == str(target.agent_id)
    assert activation_rows[0].data["task_id"] == 701
    assert activation_rows[0].data["source_task_log_id"] == task_rows[0].id


@pytest.mark.anyio
async def test_preseed_rejects_shared_paths_without_partial_writes(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Preseed is only for target colleague roots and rejects partial batches."""
    owner = await _create_user(client, "preseed-atomic")
    org_data = await _create_org(client, owner, "preseed-atomic")
    target = Assistant(
        user_id=owner["id"],
        organization_id=org_data["id"],
        first_name="Support",
        surname="Ops",
    )
    dbsession.add(target)
    dbsession.flush()

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/preseed",
        json={
            "writes": [
                {"context": "Knowledge", "entries": [{"content": "safe"}]},
                {"context": "Spaces/999/Knowledge", "entries": [{"content": "shared"}]},
            ],
        },
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
    target_knowledge_context = _assistant_context_name(target, "Knowledge")
    leaked_context = dbsession.scalar(
        select(Context).where(Context.name == target_knowledge_context),
    )
    assert leaked_context is None


@pytest.mark.anyio
async def test_preseed_requires_the_target_scope_coordinator(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Personal Coordinators cannot preseed another user's colleague."""
    owner = await _create_user(client, "preseed-personal-owner")
    other = await _create_user(client, "preseed-personal-other")
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
        f"/v0/assistant/{target.agent_id}/preseed",
        json={"writes": [{"context": "Knowledge", "entries": [{"content": "nope"}]}]},
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.json()


@pytest.mark.anyio
async def test_preseed_org_target_requires_personal_coordinator(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Org preseed requires the actor's personal coordinator to exist."""
    owner = await _create_user(client, "preseed-org-owner")
    member = await _create_user(client, "preseed-org-member")
    org_data = await _create_org(client, owner, "preseed-org-missing-personal")

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

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/preseed",
        json={"writes": [{"context": "Knowledge", "entries": [{"content": "nope"}]}]},
        headers=member["headers"],
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.json()
    assert response.json()["detail"] == "Coordinator not found."


@pytest.mark.anyio
async def test_preseed_org_target_requires_org_write_access(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Users outside the org cannot preseed org assistants."""
    owner = await _create_user(client, "preseed-org-rbac-owner")
    outsider = await _create_user(client, "preseed-org-rbac-outsider")
    org_data = await _create_org(client, owner, "preseed-org-rbac")

    target = Assistant(
        user_id=owner["id"],
        organization_id=org_data["id"],
        first_name="Finance",
        surname="Target",
    )
    dbsession.add(target)
    dbsession.commit()

    response = await client.post(
        f"/v0/assistant/{target.agent_id}/preseed",
        json={"writes": [{"context": "Knowledge", "entries": [{"content": "nope"}]}]},
        headers=outsider["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.json()
