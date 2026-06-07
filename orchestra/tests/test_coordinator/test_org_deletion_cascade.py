"""Coordinator organization deletion tests for shared-team cleanup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import sqlalchemy as sa
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantCleanupTask,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Team,
    TeamAssistantMembership,
)
from orchestra.services import task_machine_state_service, team_cleanup_service
from orchestra.tests.utils import create_test_user


class _Response:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://comms.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "Comms error",
                request=request,
                response=response,
            )


class _CommsClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[_Response] = []

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                **kwargs,
            },
        )
        if self.responses:
            return self.responses.pop(0)
        return _Response()


@pytest.fixture
def org_delete_boundaries(monkeypatch: pytest.MonkeyPatch) -> _CommsClient:
    """Keep org-deletion cascade tests inside local API and database boundaries."""

    comms_client = _CommsClient()
    monkeypatch.setattr(
        "orchestra.web.api.utils.assistant_infra.create_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.services.coordinator_service.create_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.services.team_membership_refresh_service.reawaken_assistant",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.delete_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.web.api.utils.assistant_infra.delete_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    bucket = MagicMock()
    bucket.delete_org_account_photos.return_value = 0
    bucket.delete_assistant_file.return_value = None
    bucket.delete_all_assistant_data.return_value = {
        "media": 0,
        "recordings": 0,
        "attachments": 0,
    }
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.create_bucket_service",
        MagicMock(return_value=bucket),
    )
    monkeypatch.setattr(
        "orchestra.services.assistant_cleanup_service.create_bucket_service",
        MagicMock(return_value=bucket),
    )
    monkeypatch.setattr(
        "orchestra.services.assistant_cleanup_service.delete_phone_number",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(team_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        team_cleanup_service,
        "_comms_url_for",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(
        team_cleanup_service,
        "get_async_client",
        lambda: comms_client,
    )
    return comms_client


async def _create_user(client: AsyncClient, suffix: str) -> dict:
    return await create_test_user(client, f"org-cascade-{suffix}@test.com")


async def _create_org(client: AsyncClient, owner: dict, suffix: str) -> dict:
    response = await client.post(
        "/v0/organizations",
        headers=owner["headers"],
        json={"name": f"Cascade Org {suffix}"},
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
        "coordinator_id": coordinator_response.json()["coordinator_id"],
    }


async def _create_org_team(
    client: AsyncClient,
    owner: dict,
    *,
    organization_id: int,
    name: str,
) -> dict:
    response = await client.post(
        f"/v0/organizations/{organization_id}/teams",
        headers=owner["headers"],
        json={
            "name": name,
            "description": f"{name} organization workspace for cascade cleanup.",
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()


async def _add_team_member(
    client: AsyncClient,
    owner: dict,
    *,
    organization_id: int,
    team_id: int,
    assistant_id: int,
) -> None:
    response = await client.post(
        f"/v0/organizations/{organization_id}/teams/{team_id}/assistant-members",
        headers=owner["headers"],
        json={"assistant_id": assistant_id},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()


def _make_org_assistant(
    dbsession: Session,
    *,
    owner_id: str,
    organization_id: int,
    first_name: str,
) -> Assistant:
    assistant = Assistant(
        user_id=owner_id,
        organization_id=organization_id,
        first_name=first_name,
        surname="Cascade",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _assistants_project(dbsession: Session, *, organization_id: int) -> Project:
    project = dbsession.scalar(
        sa.select(Project).where(
            Project.organization_id == organization_id,
            Project.name == "Assistants",
        ),
    )
    if project is None:
        project = Project(
            user_id=None,
            organization_id=organization_id,
            name="Assistants",
            description="Project to manage and track all organization assistants.",
            is_versioned=False,
        )
        dbsession.add(project)
        dbsession.flush()
    return project


def _add_context_log(
    dbsession: Session,
    *,
    project: Project,
    context_name: str,
    entries: dict,
) -> LogEvent:
    context = dbsession.scalar(
        sa.select(Context).where(
            Context.project_id == project.id,
            Context.name == context_name,
        ),
    )
    if context is None:
        context = Context(project_id=project.id, name=context_name)
        dbsession.add(context)
        dbsession.flush()
    log_event = LogEvent(project_id=project.id, data=entries)
    dbsession.add(log_event)
    dbsession.flush()
    dbsession.add(
        LogEventContext(log_event_id=log_event.id, context_id=context.id),
    )
    dbsession.flush()
    return log_event


def _add_scheduled_activation(
    dbsession: Session,
    *,
    project: Project,
    owner_id: str,
    assistant_id: int,
    team_id: int,
    task_id: int,
) -> None:
    _add_context_log(
        dbsession,
        project=project,
        context_name=task_machine_state_service.build_task_activation_context_name(
            f"{owner_id}/{assistant_id}/Tasks",
        ),
        entries={
            "activation_kind": "scheduled",
            "assistant_id": str(assistant_id),
            "destination": f"team:{team_id}",
            "task_id": task_id,
            "activation_revision": f"rev-{task_id}",
            "next_due_at": "2026-04-10T09:00:00+00:00",
            "execution_mode": "live",
        },
    )


def _team_context_count(dbsession: Session, team_id: int) -> int:
    team_root = f"Teams/{team_id}"
    return int(
        dbsession.scalar(
            sa.select(sa.func.count())
            .select_from(Context)
            .where((Context.name == team_root) | Context.name.like(f"{team_root}/%")),
        )
        or 0,
    )


def _org_delete_cleanup_task_count(
    dbsession: Session,
    *,
    assistant_ids: list[int],
) -> int:
    return int(
        dbsession.scalar(
            sa.select(sa.func.count())
            .select_from(AssistantCleanupTask)
            .where(
                AssistantCleanupTask.source_flow == "organization_delete",
                AssistantCleanupTask.assistant_id.in_(assistant_ids),
            ),
        )
        or 0,
    )


@pytest.mark.anyio
async def test_org_deletion_cascades_through_team_cleanup_service(
    client: AsyncClient,
    dbsession: Session,
    org_delete_boundaries: _CommsClient,
) -> None:
    """Deleting an organization cleans every owned team before dropping the org."""

    owner = await _create_user(client, "success")
    org = await _create_org(client, owner, "success")
    organization_id = org["id"]
    coordinator_id = int(org["coordinator_id"])
    first_team = await _create_org_team(
        client,
        owner,
        organization_id=organization_id,
        name="Success Shared",
    )
    first_team_id = first_team["id"]
    second_team = await _create_org_team(
        client,
        owner,
        organization_id=organization_id,
        name="Success Team",
    )
    second_team_id = second_team["id"]
    team_assistant = _make_org_assistant(
        dbsession,
        owner_id=owner["id"],
        organization_id=organization_id,
        first_name="Team",
    )
    team_assistant_id = team_assistant.agent_id
    dbsession.commit()
    await _add_team_member(
        client,
        owner,
        organization_id=organization_id,
        team_id=second_team_id,
        assistant_id=team_assistant_id,
    )

    project = _assistants_project(dbsession, organization_id=organization_id)
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Teams/{first_team_id}/Knowledge",
        entries={"fact": "shared"},
    )
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Teams/{second_team_id}/Knowledge",
        entries={"fact": "team"},
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=team_assistant_id,
        team_id=second_team_id,
        task_id=101,
    )
    dbsession.commit()

    response = await client.delete(
        f"/v0/organizations/{organization_id}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    assert [
        request["json"]["task_id"] for request in org_delete_boundaries.requests
    ] == [
        101,
    ]
    dbsession.expire_all()
    assert dbsession.get(Team, first_team_id) is None
    assert dbsession.get(Team, second_team_id) is None
    assert dbsession.get(Assistant, coordinator_id) is not None
    assert dbsession.get(Assistant, team_assistant_id) is None
    assert (
        dbsession.scalar(
            sa.select(sa.func.count())
            .select_from(TeamAssistantMembership)
            .where(
                TeamAssistantMembership.team_id.in_(
                    [first_team_id, second_team_id],
                ),
            ),
        )
        == 0
    )
    assert _team_context_count(dbsession, first_team_id) == 0
    assert _team_context_count(dbsession, second_team_id) == 0


@pytest.mark.anyio
async def test_org_deletion_retry_finishes_remaining_teams_after_partial_cleanup_failure(
    client: AsyncClient,
    dbsession: Session,
    org_delete_boundaries: _CommsClient,
) -> None:
    """Retried organization deletion resumes after completed team cleanup."""

    owner = await _create_user(client, "retry")
    org = await _create_org(client, owner, "retry")
    organization_id = org["id"]
    coordinator_id = int(org["coordinator_id"])
    first_team = await _create_org_team(
        client,
        owner,
        organization_id=organization_id,
        name="Retry Shared",
    )
    first_team_id = first_team["id"]
    second_team = await _create_org_team(
        client,
        owner,
        organization_id=organization_id,
        name="Retry Team",
    )
    second_team_id = second_team["id"]
    team_assistant = _make_org_assistant(
        dbsession,
        owner_id=owner["id"],
        organization_id=organization_id,
        first_name="Retry",
    )
    team_assistant_id = team_assistant.agent_id
    dbsession.commit()
    await _add_team_member(
        client,
        owner,
        organization_id=organization_id,
        team_id=second_team_id,
        assistant_id=team_assistant_id,
    )

    project = _assistants_project(dbsession, organization_id=organization_id)
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Teams/{first_team_id}/Knowledge",
        entries={"fact": "cleaned first"},
    )
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Teams/{second_team_id}/Knowledge",
        entries={"fact": "retry me"},
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=team_assistant_id,
        team_id=second_team_id,
        task_id=202,
    )
    dbsession.commit()
    org_delete_boundaries.responses.append(_Response(status_code=500))

    first = await client.delete(
        f"/v0/organizations/{organization_id}",
        headers=owner["headers"],
    )

    assert first.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    dbsession.expire_all()
    assert dbsession.get(Team, first_team_id) is None
    remaining_team = dbsession.get(Team, second_team_id)
    assert remaining_team is not None
    assert remaining_team.status == "deleting"
    assert _team_context_count(dbsession, first_team_id) == 0
    assert _team_context_count(dbsession, second_team_id) == 1
    assert (
        _org_delete_cleanup_task_count(
            dbsession,
            assistant_ids=[team_assistant_id],
        )
        == 0
    )

    second = await client.delete(
        f"/v0/organizations/{organization_id}",
        headers=owner["headers"],
    )

    assert second.status_code == status.HTTP_204_NO_CONTENT, second.text
    dbsession.expire_all()
    assert dbsession.get(Team, second_team_id) is None
    assert (
        dbsession.scalar(
            sa.select(sa.func.count())
            .select_from(Team)
            .where(Team.organization_id == organization_id),
        )
        == 0
    )
    assert dbsession.get(Assistant, team_assistant_id) is None
    assert dbsession.get(Assistant, coordinator_id) is not None
    assert (
        _org_delete_cleanup_task_count(
            dbsession,
            assistant_ids=[team_assistant_id],
        )
        == 1
    )
