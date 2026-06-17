"""Tests for assistant deletion across shared-team memberships."""

from __future__ import annotations

import httpx
import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    Organization,
    Project,
    Team,
    TeamAssistantMembership,
)
from orchestra.services import (
    task_machine_state_service,
    team_cleanup_service,
    team_membership_refresh_service,
)
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


def _make_team_membership(
    dbsession: Session,
    *,
    owner_id: str,
    assistant: Assistant,
    name: str,
    organization_id: int,
) -> Team:
    team = Team(
        name=name,
        description=f"{name} assistant deletion membership workspace.",
        organization_id=organization_id,
    )
    dbsession.add(team)
    dbsession.flush()
    dbsession.add(
        TeamAssistantMembership(
            assistant_id=assistant.agent_id,
            team_id=team.id,
            added_by=owner_id,
        ),
    )
    dbsession.flush()
    return team


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
    team_id: int,
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
            "destination": f"team:{team_id}",
            "task_id": task_id,
            "activation_revision": f"rev-{task_id}",
            "next_due_at": "2026-04-10T09:00:00+00:00",
            "execution_mode": "live",
        },
    )
    dbsession.add(log_event)
    dbsession.flush()
    dbsession.add(
        LogEventContext(
            project_id=log_event.project_id,
            log_event_id=log_event.id,
            context_id=context.id,
        ),
    )
    dbsession.flush()
    return log_event


def _ensure_organization(dbsession: Session, *, owner_id: str) -> Organization:
    org = (
        dbsession.query(Organization).filter(Organization.owner_id == owner_id).first()
    )
    if org is None:
        org = Organization(name="Delete Test Org", owner_id=owner_id)
        dbsession.add(org)
        dbsession.flush()
    return org


@pytest.fixture
def membership_update(monkeypatch):
    calls = []

    async def _publish(assistant_id: str, *, data=None):
        calls.append(
            {
                "assistant_id": assistant_id,
                "data": data,
            },
        )
        return {"success": True}

    monkeypatch.setattr(
        team_membership_refresh_service,
        "reawaken_assistant",
        _publish,
    )
    return calls


@pytest.fixture
def comms_client(monkeypatch):
    client = _CommsClient()
    monkeypatch.setattr(team_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        team_cleanup_service,
        "_comms_url",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(team_cleanup_service, "get_async_client", lambda: client)
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
    org = _ensure_organization(dbsession, owner_id=owner["id"])
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    assistant_id = assistant.agent_id
    first_team = _make_team_membership(
        dbsession,
        owner_id=owner["id"],
        assistant=assistant,
        name="First",
        organization_id=org.id,
    )
    second_team = _make_team_membership(
        dbsession,
        owner_id=owner["id"],
        assistant=assistant,
        name="Second",
        organization_id=org.id,
    )
    project = _ensure_assistants_project(dbsession, owner_id=owner["id"])
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant_id,
        team_id=first_team.id,
        task_id=501,
    )
    _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant_id,
        team_id=second_team.id,
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
        dbsession.query(TeamAssistantMembership)
        .filter(TeamAssistantMembership.assistant_id == assistant_id)
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
            "data": {
                "assistant_id": str(assistant_id),
                "team_ids": "[]",
                "team_summaries": "[]",
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
    monkeypatch.setattr(team_cleanup_service, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(
        team_cleanup_service,
        "_comms_url",
        lambda: "https://comms.test",
    )
    monkeypatch.setattr(team_cleanup_service, "get_async_client", lambda: comms_client)

    owner = await create_test_user(client, "assistant-delete-failure@test.com")
    org = _ensure_organization(dbsession, owner_id=owner["id"])
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    assistant_id = assistant.agent_id
    team = _make_team_membership(
        dbsession,
        owner_id=owner["id"],
        assistant=assistant,
        name="Failure",
        organization_id=org.id,
    )
    project = _ensure_assistants_project(dbsession, owner_id=owner["id"])
    activation = _add_scheduled_activation(
        dbsession,
        project=project,
        owner_id=owner["id"],
        assistant_id=assistant_id,
        team_id=team.id,
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
        dbsession.query(TeamAssistantMembership)
        .filter(
            TeamAssistantMembership.assistant_id == assistant_id,
            TeamAssistantMembership.team_id == team.id,
        )
        .one_or_none()
        is not None
    )
    assert dbsession.query(LogEvent).filter(LogEvent.id == activation.id).one_or_none()
    assert membership_update == []
