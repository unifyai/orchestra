"""Endpoint tests for Coordinator provisioning and lifecycle contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    Assistant,
    AssistantSecret,
    AssistantSpaceMembership,
    ContactMembership,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Space,
)
from orchestra.services.task_machine_state_service import (
    build_task_activation_context_name,
)
from orchestra.tests.utils import HEADERS, create_test_user

EXPECTED_COORDINATOR_DEFAULT_NATIONALITY = "United States"


@pytest.fixture(autouse=True)
def coordinator_pubsub_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Coordinator tests inside Orchestra's API/database boundary."""
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.create_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.delete_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.web.api.users.views.create_pubsub_topic",
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
    return response.json()


def _assistant_context_name(coordinator: Assistant, suffix: str) -> str:
    return f"{coordinator.user_id}/{coordinator.agent_id}/{suffix}"


def _assistants_project(
    dbsession: Session,
    *,
    coordinator: Assistant,
) -> Project:
    return dbsession.scalar(
        select(Project).where(
            Project.organization_id == coordinator.organization_id,
            Project.name == "Assistants",
        ),
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


@pytest.mark.anyio
async def test_create_organization_provisions_coordinator_and_org_default_space(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Organization creation provisions the Coordinator, default space, and grants."""
    owner = await _create_user(client, "org-provision")

    org_data = await _create_org(client, owner, "provision")
    coordinator_id = int(org_data["coordinator_id"])

    coordinator = dbsession.get(Assistant, coordinator_id)
    assert coordinator is not None
    assert coordinator.is_coordinator is True
    assert coordinator.organization_id == org_data["id"]
    assert coordinator.user_id == owner["id"]
    assert coordinator.nationality == EXPECTED_COORDINATOR_DEFAULT_NATIONALITY
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

    space = dbsession.scalar(
        select(Space).where(
            Space.organization_id == org_data["id"],
            Space.kind == "org_default",
        ),
    )
    assert space is not None
    assert space.name == org_data["name"]
    assert space.owner_user_id == owner["id"]

    membership = dbsession.scalar(
        select(AssistantSpaceMembership).where(
            AssistantSpaceMembership.assistant_id == coordinator.agent_id,
            AssistantSpaceMembership.space_id == space.space_id,
        ),
    )
    assert membership is not None

    resource_access_dao = ResourceAccessDAO(dbsession)
    assert resource_access_dao.check_user_permission(
        owner["id"],
        "assistant",
        coordinator.agent_id,
        "assistant:write",
    )


@pytest.mark.anyio
async def test_transcript_seed_is_idempotent_by_assistant_row(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Retried opener seeding returns the first assistant transcript row."""
    owner = await _create_user(client, "seed")
    org_data = await _create_org(client, owner, "seed")
    coordinator_id = int(org_data["coordinator_id"])

    first = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Welcome to your Coordinator."},
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )
    assert first.status_code == status.HTTP_200_OK, first.json()
    first_id = first.json()["info"]["log_event_id"]

    second = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "A different opener should not duplicate."},
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
    )
    assert second.status_code == status.HTTP_200_OK, second.json()
    assert second.json()["info"]["log_event_id"] == first_id

    coordinator = dbsession.get(Assistant, coordinator_id)
    project = _assistants_project(dbsession, coordinator=coordinator)
    transcripts = _context(
        dbsession,
        project=project,
        name=_assistant_context_name(coordinator, "Transcripts"),
    )
    logs = dbsession.scalars(
        select(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .where(LogEventContext.context_id == transcripts.id),
    ).all()
    assert [log.data["role"] for log in logs] == ["assistant"]


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
        ("Coordinator/State", {"mode": "ready_to_go"}),
        ("Coordinator/Checklist", {"title": "Connect HubSpot"}),
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
    assert first.status_code == status.HTTP_201_CREATED, first.json()
    coordinator_id = first.json()["coordinator_id"]
    coordinator = dbsession.get(Assistant, int(coordinator_id))
    assert coordinator is not None
    assert coordinator.nationality == EXPECTED_COORDINATOR_DEFAULT_NATIONALITY
    coordinator.nationality = None
    dbsession.commit()

    second = await client.post(
        f"/v0/user/{owner['id']}/coordinator",
        headers=owner["headers"],
    )
    assert second.status_code == status.HTTP_200_OK, second.json()
    assert second.json()["coordinator_id"] == coordinator_id
    dbsession.refresh(coordinator)
    assert coordinator.nationality == EXPECTED_COORDINATOR_DEFAULT_NATIONALITY

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
async def test_coordinator_admin_gate_and_direct_delete_guard(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Resource write permission is insufficient without Admin/Owner org role."""
    owner = await _create_user(client, "admin-gate-owner")
    member = await _create_user(client, "admin-gate-member")
    admin = await _create_user(client, "admin-gate-admin")
    org_data = await _create_org(client, owner, "admin-gate")
    coordinator_id = int(org_data["coordinator_id"])

    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
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
    ResourceAccessDAO(dbsession).grant_access(
        resource_type="assistant",
        resource_id=coordinator_id,
        role_id=owner_role.id,
        grantee_type="user",
        grantee_id=member["id"],
    )
    dbsession.flush()

    forbidden_seed = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Member should not seed."},
        headers=member["headers"],
    )
    assert (
        forbidden_seed.status_code == status.HTTP_403_FORBIDDEN
    ), forbidden_seed.json()
    assert forbidden_seed.json()["detail"] == "admin_required"

    forbidden_reset = await client.post(
        f"/v0/assistant/{coordinator_id}/reset",
        headers=member["headers"],
    )
    assert (
        forbidden_reset.status_code == status.HTTP_403_FORBIDDEN
    ), forbidden_reset.json()
    assert forbidden_reset.json()["detail"] == "admin_required"

    admin_seed = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Admin can seed."},
        headers=admin["headers"],
    )
    assert admin_seed.status_code == status.HTTP_200_OK, admin_seed.json()

    admin_reset = await client.post(
        f"/v0/assistant/{coordinator_id}/reset",
        headers=admin["headers"],
    )
    assert admin_reset.status_code == status.HTTP_200_OK, admin_reset.json()

    org_member_dao.update_member_role(
        user_id=member["id"],
        organization_id=org_data["id"],
        role_id=admin_role.id,
    )
    promoted_seed = await client.post(
        f"/v0/assistant/{coordinator_id}/transcript-seed",
        json={"content": "Promoted admin can seed."},
        headers=member["headers"],
    )
    assert promoted_seed.status_code == status.HTTP_200_OK, promoted_seed.json()

    delete = await client.delete(
        f"/v0/assistant/{coordinator_id}",
        headers={"Authorization": f"Bearer {org_data['api_key']}"},
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

    coordinator = dbsession.get(Assistant, coordinator_id)
    project = _assistants_project(dbsession, coordinator=coordinator)
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
    coordinator = dbsession.get(Assistant, int(org_data["coordinator_id"]))
    project = _assistants_project(dbsession, coordinator=coordinator)
    assert (
        _context(
            dbsession,
            project=project,
            name=_assistant_context_name(target, "Knowledge"),
        )
        is None
    )


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
    assert coordinator_response.status_code == status.HTTP_201_CREATED
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
