"""Route-level tests for shared space cleanup."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Space,
)
from orchestra.services import (
    space_cleanup_service,
    space_membership_refresh_service,
    task_machine_state_service,
)
from orchestra.tests.utils import create_test_user


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {"success": True, "deleted": True}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://comms.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "Comms error",
                request=request,
                response=response,
            )

    def json(self) -> dict:
        return self._payload


class _CommsClient:
    def __init__(self, responses: list[_Response] | None = None):
        self.requests: list[dict] = []
        self._responses = responses or []

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                **kwargs,
            },
        )
        if self._responses:
            return self._responses.pop(0)
        return _Response()


def _make_assistant(
    dbsession: Session,
    *,
    owner_id: str,
    first_name: str = "Space",
) -> Assistant:
    assistant = Assistant(
        user_id=owner_id,
        first_name=first_name,
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


async def _create_space(client: AsyncClient, headers: dict, name: str) -> dict:
    response = await client.post(
        "/v0/spaces",
        headers=headers,
        json={
            "name": name,
            "description": f"{name} shared workspace for cleanup tests",
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()


def _ensure_assistants_project(dbsession: Session, *, owner_id: str) -> Project:
    project = (
        dbsession.query(Project)
        .filter(Project.user_id == owner_id, Project.name == "Assistants")
        .one_or_none()
    )
    if project is None:
        project = Project(user_id=owner_id, name="Assistants")
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
    context = (
        dbsession.query(Context)
        .filter(Context.project_id == project.id, Context.name == context_name)
        .one_or_none()
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
) -> LogEvent:
    return _add_context_log(
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


@pytest.fixture
def comms_client(monkeypatch):
    client = _CommsClient()
    monkeypatch.setattr(space_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        space_cleanup_service,
        "_comms_url_for",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(space_cleanup_service, "get_async_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def reawaken_assistant_mock(monkeypatch) -> AsyncMock:
    """Prevent cleanup tests from calling live assistant-update webhooks."""

    mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(
        space_membership_refresh_service,
        "reawaken_assistant",
        mock,
    )
    return mock


@pytest.mark.anyio
async def test_delete_space_cascades_shared_contexts_and_space_rows(
    client: AsyncClient,
    dbsession: Session,
    comms_client: _CommsClient,
    reawaken_assistant_mock: AsyncMock,
) -> None:
    """Deleting a space cancels deliveries and removes only shared roots."""

    owner = await create_test_user(client, "space-cleanup-owner@test.com")
    first_assistant = _make_assistant(dbsession, owner_id=owner["id"], first_name="One")
    second_assistant = _make_assistant(
        dbsession,
        owner_id=owner["id"],
        first_name="Two",
    )
    space = await _create_space(client, owner["headers"], "Cleanup")
    for assistant in (first_assistant, second_assistant):
        add_member = await client.post(
            f"/v0/spaces/{space['space_id']}/members",
            headers=owner["headers"],
            json={"assistant_id": assistant.agent_id},
        )
        assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()
    reawaken_assistant_mock.reset_mock()

    project = _ensure_assistants_project(dbsession, owner_id=owner["id"])
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Spaces/{space['space_id']}/Knowledge",
        entries={"fact": "shared"},
    )
    first_activation = _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=first_assistant.agent_id,
        space_id=space["space_id"],
        task_id=101,
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=second_assistant.agent_id,
        space_id=space["space_id"],
        task_id=202,
    )
    dbsession.commit()

    response = await client.delete(
        f"/v0/spaces/{space['space_id']}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    assert [request["json"]["task_id"] for request in comms_client.requests] == [
        101,
        202,
    ]
    assert [
        call.kwargs["data"] for call in reawaken_assistant_mock.await_args_list
    ] == [
        {
            "assistant_id": str(first_assistant.agent_id),
            "space_ids": json.dumps([]),
            "space_summaries": json.dumps([]),
            "update_kind": "membership",
        },
        {
            "assistant_id": str(second_assistant.agent_id),
            "space_ids": json.dumps([]),
            "space_summaries": json.dumps([]),
            "update_kind": "membership",
        },
    ]
    assert (
        dbsession.query(Context)
        .filter(Context.name.like(f"Spaces/{space['space_id']}%"))
        .count()
        == 0
    )
    assert (
        dbsession.query(LogEvent)
        .filter(LogEvent.id == first_activation.id)
        .one_or_none()
        is not None
    )
    assert dbsession.get(Space, space["space_id"]) is None
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(AssistantSpaceMembership.space_id == space["space_id"])
        .count()
        == 0
    )


@pytest.mark.anyio
async def test_delete_space_phase_2_failure_leaves_retryable_deleting_space(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch,
) -> None:
    """Delivery cancellation failures preserve shared data for retry."""

    comms_client = _CommsClient([_Response(status_code=500)])
    monkeypatch.setattr(space_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        space_cleanup_service,
        "_comms_url_for",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(space_cleanup_service, "get_async_client", lambda: comms_client)

    owner = await create_test_user(client, "space-cleanup-phase2@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    space = await _create_space(client, owner["headers"], "Cleanup Failure")
    add_member = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()

    project = _ensure_assistants_project(dbsession, owner_id=owner["id"])
    _add_context_log(
        dbsession,
        project=project,
        context_name=f"Spaces/{space['space_id']}/Knowledge",
        entries={"fact": "shared"},
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant.agent_id,
        space_id=space["space_id"],
        task_id=303,
    )
    dbsession.commit()

    response = await client.delete(
        f"/v0/spaces/{space['space_id']}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json()["phase"] == 2
    dbsession.expire_all()
    assert dbsession.get(Space, space["space_id"]).status == "deleting"
    assert (
        dbsession.query(Context)
        .filter(Context.name == f"Spaces/{space['space_id']}/Knowledge")
        .one_or_none()
        is not None
    )
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(AssistantSpaceMembership.space_id == space["space_id"])
        .count()
        == 1
    )


@pytest.mark.anyio
async def test_delete_space_phase_3_failure_keeps_memberships(
    client: AsyncClient,
    dbsession: Session,
    comms_client: _CommsClient,
    monkeypatch,
) -> None:
    """Shared-context purge failures do not drop memberships."""

    owner = await create_test_user(client, "space-cleanup-phase3@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    space = await _create_space(client, owner["headers"], "Context Failure")
    add_member = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()
    dbsession.commit()

    def _fail_context_purge(session: Session, *, space_id: int) -> None:
        raise RuntimeError("context purge failed")

    monkeypatch.setattr(
        space_cleanup_service,
        "_purge_shared_space_contexts",
        _fail_context_purge,
    )

    response = await client.delete(
        f"/v0/spaces/{space['space_id']}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json()["phase"] == 3
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(AssistantSpaceMembership.space_id == space["space_id"])
        .count()
        == 1
    )
    assert dbsession.get(Space, space["space_id"]) is not None
    assert comms_client.requests == []


@pytest.mark.anyio
async def test_delete_space_accepts_already_absent_activation_envelope(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch,
) -> None:
    """Communication's deleted=false success envelope keeps cleanup idempotent."""

    comms_client = _CommsClient(
        [_Response(payload={"success": True, "deleted": False})],
    )
    monkeypatch.setattr(space_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        space_cleanup_service,
        "_comms_url_for",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(space_cleanup_service, "get_async_client", lambda: comms_client)

    owner = await create_test_user(client, "space-cleanup-absent@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    space = await _create_space(client, owner["headers"], "Absent Activation")
    add_member = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()
    project = _ensure_assistants_project(dbsession, owner_id=owner["id"])
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant.agent_id,
        space_id=space["space_id"],
        task_id=404,
    )
    dbsession.commit()

    response = await client.delete(
        f"/v0/spaces/{space['space_id']}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    assert dbsession.get(Space, space["space_id"]) is None
