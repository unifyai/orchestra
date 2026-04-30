"""Tests for assistant deletion across shared-space memberships."""

from __future__ import annotations

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
    def __init__(
        self,
        responses: list[_Response] | None = None,
        default_response: _Response | None = None,
    ):
        self.requests: list[dict] = []
        self._responses = responses or []
        self._default_response = default_response or _Response()

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
        return self._default_response


def _make_assistant(dbsession: Session, *, owner_id: str) -> Assistant:
    assistant = Assistant(
        user_id=owner_id,
        first_name="Delete",
        surname="Member",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_space_membership(
    dbsession: Session,
    *,
    owner_id: str,
    assistant: Assistant,
    name: str,
) -> Space:
    space = Space(name=name, owner_user_id=owner_id)
    dbsession.add(space)
    dbsession.flush()
    dbsession.add(
        AssistantSpaceMembership(
            assistant_id=assistant.agent_id,
            space_id=space.space_id,
            added_by=owner_id,
        ),
    )
    dbsession.flush()
    return space


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


def _add_scheduled_activation(
    dbsession: Session,
    *,
    project: Project,
    owner_id: str,
    assistant_id: int,
    space_id: int,
    task_id: int,
) -> LogEvent:
    context_name = task_machine_state_service.build_task_activation_context_name(
        f"{owner_id}/{assistant_id}/Tasks",
    )
    context = (
        dbsession.query(Context)
        .filter(Context.project_id == project.id, Context.name == context_name)
        .one_or_none()
    )
    if context is None:
        context = Context(project_id=project.id, name=context_name)
        dbsession.add(context)
        dbsession.flush()
    log_event = LogEvent(
        project_id=project.id,
        data={
            "activation_kind": "scheduled",
            "assistant_id": str(assistant_id),
            "destination": f"space:{space_id}",
            "task_id": task_id,
            "activation_revision": f"rev-{task_id}",
            "next_due_at": "2026-04-10T09:00:00+00:00",
            "execution_mode": "live",
        },
    )
    dbsession.add(log_event)
    dbsession.flush()
    dbsession.add(LogEventContext(log_event_id=log_event.id, context_id=context.id))
    dbsession.flush()
    return log_event


@pytest.fixture
def membership_update(monkeypatch):
    calls = []

    async def _publish(assistant_id: str, deploy_env=None, *, data=None):
        calls.append(
            {
                "assistant_id": assistant_id,
                "deploy_env": deploy_env,
                "data": data,
            },
        )
        return {"success": True}

    monkeypatch.setattr(space_cleanup_service, "reawaken_assistant", _publish)
    return calls


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


@pytest.mark.anyio
async def test_delete_assistant_cleans_memberships_before_row_delete(
    client: AsyncClient,
    dbsession: Session,
    comms_client: _CommsClient,
    membership_update,
) -> None:
    """Assistant deletion removes membership-owned state before deleting the row."""

    owner = await create_test_user(client, "assistant-delete-member@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    assistant_id = assistant.agent_id
    assistant_deploy_env = assistant.deploy_env
    first_space = _make_space_membership(
        dbsession,
        owner_id=owner["id"],
        assistant=assistant,
        name="First",
    )
    second_space = _make_space_membership(
        dbsession,
        owner_id=owner["id"],
        assistant=assistant,
        name="Second",
    )
    project = _ensure_assistants_project(dbsession, owner_id=owner["id"])
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant_id,
        space_id=first_space.space_id,
        task_id=501,
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant_id,
        space_id=second_space.space_id,
        task_id=502,
    )
    dbsession.commit()

    response = await client.delete(
        f"/v0/assistant/{assistant_id}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert dbsession.get(Assistant, assistant_id) is None
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(AssistantSpaceMembership.assistant_id == assistant_id)
        .count()
        == 0
    )
    assert [request["json"]["task_id"] for request in comms_client.requests] == [
        501,
        502,
    ]
    assert membership_update == [
        {
            "assistant_id": str(assistant_id),
            "deploy_env": assistant_deploy_env,
            "data": {
                "assistant_id": str(assistant_id),
                "space_ids": "[]",
                "update_kind": "membership",
            },
        },
    ]


@pytest.mark.anyio
async def test_delete_assistant_membership_cleanup_failure_rolls_back(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch,
    membership_update,
) -> None:
    """A failed membership cleanup leaves the assistant and membership retryable."""

    comms_client = _CommsClient(default_response=_Response(status_code=500))
    monkeypatch.setattr(space_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        space_cleanup_service,
        "_comms_url_for",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(space_cleanup_service, "get_async_client", lambda: comms_client)

    owner = await create_test_user(client, "assistant-delete-failure@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    assistant_id = assistant.agent_id
    space = _make_space_membership(
        dbsession,
        owner_id=owner["id"],
        assistant=assistant,
        name="Failure",
    )
    project = _ensure_assistants_project(dbsession, owner_id=owner["id"])
    activation = _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant_id,
        space_id=space.space_id,
        task_id=503,
    )
    dbsession.commit()

    response = await client.delete(
        f"/v0/assistant/{assistant_id}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    dbsession.expire_all()
    assert dbsession.get(Assistant, assistant_id) is not None
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == assistant_id,
            AssistantSpaceMembership.space_id == space.space_id,
        )
        .one_or_none()
        is not None
    )
    assert dbsession.query(LogEvent).filter(LogEvent.id == activation.id).one_or_none()
    assert membership_update == []
