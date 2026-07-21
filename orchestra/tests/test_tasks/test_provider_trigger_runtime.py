"""Integration tests for provider-event runtime persistence and projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Context, LogEvent, Project
from orchestra.db.models.provider_trigger_models import EventTriggerBinding
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    build_task_executions_context_name,
)
from orchestra.services.task_mutation_contract import format_task_etag
from orchestra.tests.provider_triggers.conftest import (
    stub_healthy_provider_trigger_topology,
)
from orchestra.tests.provider_triggers.control_plane_harness import (
    seed_provider_event_fixture_prerequisites,
)
from orchestra.tests.test_tasks.test_trigger_task import _auth_user_id
from orchestra.tests.utils import HEADERS

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "task_trigger_contract"
)


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls():
    """Avoid hosted infra wake calls during assistant create in unit tests."""
    from unittest.mock import AsyncMock, patch

    with (
        patch(
            "orchestra.web.api.assistant.views.wake_up_assistant",
            new_callable=AsyncMock,
        ) as mock_wake_up,
        patch(
            "orchestra.web.api.assistant.views.reawaken_assistant",
            new_callable=AsyncMock,
        ) as mock_reawaken,
    ):
        mock_wake_up.return_value.status_code = 200
        mock_reawaken.return_value.status_code = 200
        mock_reawaken.return_value.json.return_value = {}
        yield


def _provider_event_trigger(*, state: str = "enabled") -> dict:
    payload = json.loads(
        (_FIXTURE_DIR / "task_trigger.provider_event.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    payload["state"] = state
    return payload


def _provider_event_task_payload(*, state: str = "enabled") -> dict:
    return {
        "name": "GitHub issue triage",
        "description": "Triage new GitHub issues for the assistant owner.",
        "status": "triggerable",
        "trigger": _provider_event_trigger(state=state),
        "enabled": True,
        "offline": False,
        "priority": "normal",
    }


@pytest.fixture
async def assistant_id(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    stub_healthy_provider_trigger_topology(monkeypatch)
    monkeypatch.setenv("PROVIDER_TRIGGER_CATALOG_ENVIRONMENT", "selfhost")
    response = await client.post(
        "/v0/assistant",
        json={"first_name": "Provider", "surname": "Runtime", "create_infra": False},
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    agent_id = int(response.json()["info"]["agent_id"])
    seed_provider_event_fixture_prerequisites(dbsession, assistant_id=agent_id)
    dbsession.commit()
    return agent_id


async def _create_provider_event_task(
    client: AsyncClient,
    *,
    assistant_id: int,
    state: str = "enabled",
) -> dict:
    response = await client.post(
        f"/v0/assistants/{assistant_id}/tasks",
        json=_provider_event_task_payload(state=state),
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()["info"]


@pytest.mark.anyio
async def test_create_persists_binding_and_projects_provider_event_execution(
    client: AsyncClient,
    assistant_id: int,
    dbsession: Session,
) -> None:
    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == created["provider_event_binding_id"],
        ),
    ).scalar_one()

    assert binding.task_revision == 1
    assert binding.desired_trigger_state == "enabled"
    assert binding.local_acceptance_open is False
    assert binding.runtime_health == "provisioning"
    assert binding.connection_id == created["trigger"]["connection_id"]
    assert binding.provider_trigger_slug == created["trigger"]["provider_trigger_slug"]
    assert (
        dict(binding.trigger_config_json or {}) == created["trigger"]["trigger_config"]
    )

    user_id = _auth_user_id()
    executions_context = build_task_executions_context_name(
        f"{user_id}/{assistant_id}/Tasks",
    )
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "context": executions_context,
        },
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    executions = response.json()["logs"]
    assert len(executions) == 1
    execution = executions[0]["entries"]
    assert execution["wake"] == "provider_event"
    assert (
        execution["provider_event_binding_id"] == created["provider_event_binding_id"]
    )
    assert execution["revision"] == binding.desired_activation_revision
    assert execution["connection_id"] == created["trigger"]["connection_id"]


@pytest.mark.anyio
async def test_paused_provider_event_task_projects_no_execution(
    client: AsyncClient,
    assistant_id: int,
) -> None:
    created = await _create_provider_event_task(
        client,
        assistant_id=assistant_id,
        state="paused",
    )
    user_id = _auth_user_id()
    executions_context = build_task_executions_context_name(
        f"{user_id}/{assistant_id}/Tasks",
    )
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "context": executions_context,
        },
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["logs"] == []
    assert created["trigger"]["state"] == "paused"


@pytest.mark.anyio
async def test_pause_removes_execution_and_closes_acceptance(
    client: AsyncClient,
    assistant_id: int,
    dbsession: Session,
) -> None:
    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    paused = await client.post(
        f"/v0/assistants/{assistant_id}/tasks/{created['task_id']}/pause",
        headers={**HEADERS, "If-Match": format_task_etag(created["task_revision"])},
    )
    assert paused.status_code == status.HTTP_200_OK, paused.json()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == created["provider_event_binding_id"],
        ),
    ).scalar_one()
    assert binding.desired_trigger_state == "paused"
    assert binding.local_acceptance_open is False

    user_id = _auth_user_id()
    executions_context = build_task_executions_context_name(
        f"{user_id}/{assistant_id}/Tasks",
    )
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "context": executions_context,
        },
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["logs"] == []


@pytest.mark.anyio
async def test_trigger_health_and_retry_use_durable_binding_state(
    client: AsyncClient,
    assistant_id: int,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Health overlays binding state with deployment topology; keep topology healthy
    # so this test asserts durable binding runtime health, not env prerequisites.
    stub_healthy_provider_trigger_topology(monkeypatch)
    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    health = await client.get(
        f"/v0/assistants/{assistant_id}/tasks/{created['task_id']}/trigger-health",
        headers=HEADERS,
    )
    assert health.status_code == status.HTTP_200_OK, health.json()
    body = health.json()["info"]
    assert body["runtime_health"] == "provisioning"
    assert body["desired_revision"] is not None
    assert body["local_acceptance_open"] is False

    retry = await client.post(
        f"/v0/assistants/{assistant_id}/tasks/{created['task_id']}/retry-trigger",
        headers=HEADERS,
    )
    assert retry.status_code == status.HTTP_202_ACCEPTED, retry.text

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == created["provider_event_binding_id"],
        ),
    ).scalar_one()
    assert binding.reconcile_next_retry_at is not None
    assert binding.reconcile_attempt_count >= 1


@pytest.mark.anyio
async def test_delete_tombstones_binding_and_removes_execution(
    client: AsyncClient,
    assistant_id: int,
    dbsession: Session,
) -> None:
    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    deleted = await client.delete(
        f"/v0/assistants/{assistant_id}/tasks/{created['task_id']}",
        headers={**HEADERS, "If-Match": format_task_etag(created["task_revision"])},
    )
    assert deleted.status_code == status.HTTP_204_NO_CONTENT

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == created["provider_event_binding_id"],
        ),
    ).scalar_one()
    assert binding.tombstoned_at is not None
    assert binding.runtime_health == "removing"
    assert binding.local_acceptance_open is False

    user_id = _auth_user_id()
    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == user_id,
            Project.organization_id.is_(None),
            Project.name == TASK_MACHINE_PROJECT_NAME,
        )
        .one()
    )
    context_name = f"{user_id}/{assistant_id}/Tasks"
    context = (
        dbsession.query(Context)
        .filter(Context.project_id == project.id, Context.name == context_name)
        .one()
    )
    remaining = (
        dbsession.query(LogEvent)
        .filter(
            LogEvent.project_id == project.id,
            LogEvent.id == created["log_event_id"],
        )
        .all()
    )
    assert remaining == []


# Duplicate receipt/run/dispatch adoption is covered by the signed ingress
# production-path suite in test_signed_ingress_acceptance.py.
