"""Coordinator organization deletion tests for shared-space cleanup."""

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
    AssistantSpaceMembership,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Space,
)
from orchestra.services import space_cleanup_service, task_machine_state_service
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
        "orchestra.web.api.organization.views.create_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.delete_pubsub_topic",
        AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.process_assistant_cleanup_tasks",
        AsyncMock(
            return_value={
                "processed": 1,
                "completed": 1,
                "retried": 0,
                "failed": 0,
                "errors": [],
            },
        ),
    )
    bucket = MagicMock()
    bucket.delete_org_account_photos.return_value = 0
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.BucketService",
        MagicMock(return_value=bucket),
    )
    monkeypatch.setattr(space_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        space_cleanup_service,
        "_comms_url_for",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(
        space_cleanup_service,
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
    return response.json()


async def _create_org_space(
    client: AsyncClient,
    owner: dict,
    *,
    organization_id: int,
    name: str,
) -> dict:
    response = await client.post(
        "/v0/spaces",
        headers=owner["headers"],
        json={"name": name, "organization_id": organization_id},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()


async def _add_space_member(
    client: AsyncClient,
    owner: dict,
    *,
    space_id: int,
    assistant_id: int,
) -> None:
    response = await client.post(
        f"/v0/spaces/{space_id}/members",
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


def _org_default_space(dbsession: Session, *, organization_id: int) -> Space:
    space = dbsession.scalar(
        sa.select(Space).where(
            Space.organization_id == organization_id,
            Space.kind == "org_default",
        ),
    )
    assert space is not None
    return space


def _assistants_project(dbsession: Session, *, organization_id: int) -> Project:
    project = dbsession.scalar(
        sa.select(Project).where(
            Project.organization_id == organization_id,
            Project.name == "Assistants",
        ),
    )
    assert project is not None
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
    space_id: int,
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
            "destination": f"space:{space_id}",
            "task_id": task_id,
            "activation_revision": f"rev-{task_id}",
            "next_due_at": "2026-04-10T09:00:00+00:00",
            "execution_mode": "live",
        },
    )


def _space_context_count(dbsession: Session, space_id: int) -> int:
    space_root = f"Spaces/{space_id}"
    return int(
        dbsession.scalar(
            sa.select(sa.func.count())
            .select_from(Context)
            .where((Context.name == space_root) | Context.name.like(f"{space_root}/%")),
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
async def test_org_deletion_cascades_through_space_cleanup_service(
    client: AsyncClient,
    dbsession: Session,
    org_delete_boundaries: _CommsClient,
) -> None:
    """Deleting an organization cleans every owned space before dropping the org."""

    owner = await _create_user(client, "success")
    org = await _create_org(client, owner, "success")
    organization_id = org["id"]
    coordinator_id = int(org["coordinator_id"])
    org_default = _org_default_space(dbsession, organization_id=organization_id)
    org_default_space_id = org_default.space_id
    team_space = await _create_org_space(
        client,
        owner,
        organization_id=organization_id,
        name="Success Team",
    )
    team_space_id = team_space["space_id"]
    team_assistant = _make_org_assistant(
        dbsession,
        owner_id=owner["id"],
        organization_id=organization_id,
        first_name="Team",
    )
    team_assistant_id = team_assistant.agent_id
    dbsession.commit()
    await _add_space_member(
        client,
        owner,
        space_id=team_space_id,
        assistant_id=team_assistant_id,
    )

    project = _assistants_project(dbsession, organization_id=organization_id)
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Spaces/{org_default_space_id}/Knowledge",
        entries={"fact": "org-wide"},
    )
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Spaces/{team_space_id}/Knowledge",
        entries={"fact": "team"},
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=team_assistant_id,
        space_id=team_space_id,
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
    assert dbsession.get(Space, org_default_space_id) is None
    assert dbsession.get(Space, team_space_id) is None
    assert dbsession.get(Assistant, coordinator_id) is None
    assert dbsession.get(Assistant, team_assistant_id) is None
    assert (
        dbsession.scalar(
            sa.select(sa.func.count())
            .select_from(AssistantSpaceMembership)
            .where(
                AssistantSpaceMembership.space_id.in_(
                    [org_default_space_id, team_space_id],
                ),
            ),
        )
        == 0
    )
    assert _space_context_count(dbsession, org_default_space_id) == 0
    assert _space_context_count(dbsession, team_space_id) == 0


@pytest.mark.anyio
async def test_org_deletion_retry_finishes_remaining_spaces_after_partial_cleanup_failure(
    client: AsyncClient,
    dbsession: Session,
    org_delete_boundaries: _CommsClient,
) -> None:
    """Retried organization deletion resumes after completed space cleanup."""

    owner = await _create_user(client, "retry")
    org = await _create_org(client, owner, "retry")
    organization_id = org["id"]
    coordinator_id = int(org["coordinator_id"])
    org_default = _org_default_space(dbsession, organization_id=organization_id)
    org_default_space_id = org_default.space_id
    team_space = await _create_org_space(
        client,
        owner,
        organization_id=organization_id,
        name="Retry Team",
    )
    team_space_id = team_space["space_id"]
    team_assistant = _make_org_assistant(
        dbsession,
        owner_id=owner["id"],
        organization_id=organization_id,
        first_name="Retry",
    )
    team_assistant_id = team_assistant.agent_id
    dbsession.commit()
    await _add_space_member(
        client,
        owner,
        space_id=team_space_id,
        assistant_id=team_assistant_id,
    )

    project = _assistants_project(dbsession, organization_id=organization_id)
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Spaces/{org_default_space_id}/Knowledge",
        entries={"fact": "cleaned first"},
    )
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Spaces/{team_space_id}/Knowledge",
        entries={"fact": "retry me"},
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=team_assistant_id,
        space_id=team_space_id,
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
    assert dbsession.get(Space, org_default_space_id) is None
    remaining_space = dbsession.get(Space, team_space_id)
    assert remaining_space is not None
    assert remaining_space.status == "deleting"
    assert _space_context_count(dbsession, org_default_space_id) == 0
    assert _space_context_count(dbsession, team_space_id) == 1
    assert (
        _org_delete_cleanup_task_count(
            dbsession,
            assistant_ids=[coordinator_id, team_assistant_id],
        )
        == 0
    )

    second = await client.delete(
        f"/v0/organizations/{organization_id}",
        headers=owner["headers"],
    )

    assert second.status_code == status.HTTP_204_NO_CONTENT, second.text
    dbsession.expire_all()
    assert dbsession.get(Space, team_space_id) is None
    assert (
        dbsession.scalar(
            sa.select(sa.func.count())
            .select_from(Space)
            .where(Space.organization_id == organization_id),
        )
        == 0
    )
    assert dbsession.get(Assistant, team_assistant_id) is None
    assert (
        _org_delete_cleanup_task_count(
            dbsession,
            assistant_ids=[coordinator_id, team_assistant_id],
        )
        == 2
    )
