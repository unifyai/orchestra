"""Integration tests for typed Tasks API revision CAS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import LogEvent, Project
from orchestra.db.models.provider_trigger_models import EventTriggerBinding
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


def _provider_event_trigger() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "task_trigger.provider_event.v1.json").read_text(
            encoding="utf-8",
        ),
    )


def _provider_event_task_payload(*, name: str = "GitHub issue triage") -> dict:
    return {
        "name": name,
        "description": "Triage new GitHub issues for the assistant owner.",
        "status": "triggerable",
        "trigger": _provider_event_trigger(),
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
        json={"first_name": "Typed", "surname": "Tasks", "create_infra": False},
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
) -> dict:
    response = await client.post(
        f"/v0/assistants/{assistant_id}/tasks",
        json=_provider_event_task_payload(),
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    body = response.json()["info"]
    assert body["task_revision"] == 1
    assert response.headers["ETag"] == '"1"'
    return body


@pytest.mark.anyio
async def test_create_provider_event_task_starts_at_revision_one(
    client: AsyncClient,
    assistant_id: int,
) -> None:
    body = await _create_provider_event_task(client, assistant_id=assistant_id)
    assert body["trigger"]["kind"] == "provider_event"
    assert body["provider_event_binding_id"]


@pytest.mark.anyio
async def test_patch_with_stale_if_match_returns_task_revision_conflict(
    client: AsyncClient,
    assistant_id: int,
) -> None:
    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    stale = await client.patch(
        f"/v0/assistants/{assistant_id}/tasks/{created['task_id']}",
        headers={**HEADERS, "If-Match": '"1"'},
        json={"description": "First writer wins."},
    )
    assert stale.status_code == status.HTTP_200_OK, stale.json()

    conflict = await client.patch(
        f"/v0/assistants/{assistant_id}/tasks/{created['task_id']}",
        headers={**HEADERS, "If-Match": '"1"'},
        json={"description": "Second writer loses."},
    )
    assert conflict.status_code == status.HTTP_409_CONFLICT, conflict.json()
    assert conflict.json()["detail"]["code"] == "task_revision_conflict"
    assert conflict.json()["detail"]["task_revision"] == 2


@pytest.mark.anyio
async def test_pause_increments_revision_and_advances_binding_epoch(
    client: AsyncClient,
    assistant_id: int,
    dbsession: Session,
) -> None:
    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    binding_id = created["provider_event_binding_id"]
    before = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    before_epoch = before.acceptance_epoch

    paused = await client.post(
        f"/v0/assistants/{assistant_id}/tasks/{created['task_id']}/pause",
        headers={**HEADERS, "If-Match": format_task_etag(created["task_revision"])},
    )
    assert paused.status_code == status.HTTP_200_OK, paused.json()
    assert paused.json()["info"]["task_revision"] == 2
    assert paused.json()["info"]["trigger"]["state"] == "paused"

    after = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert after.task_revision == 2
    assert after.acceptance_epoch == before_epoch + 1
    assert after.local_acceptance_open is False


from orchestra.services.task_mutation_contract import format_task_etag


async def test_retired_run_state_fields_are_rejected_and_bump_nothing(
    client: AsyncClient,
    assistant_id: int,
    dbsession: Session,
) -> None:
    """Definitions no longer carry run state, so writing it is unclassified.

    ``status`` and ``activated_by`` used to be runtime fields that updated
    without bumping the authored revision. Run state lives on
    ``Tasks/Executions`` now and ``RuntimeTaskField`` is deliberately empty, so
    the write must be refused outright — accepting it would quietly recreate
    the shared mutable status this split removed — and the definition must be
    left untouched, revision included.
    """

    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    user_id = _auth_user_id()
    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == user_id,
            Project.organization_id.is_(None),
            Project.name == "Assistants",
        )
        .one()
    )
    context_name = f"{user_id}/{assistant_id}/Tasks"

    response = await client.put(
        "/v0/logs",
        json={
            "logs": [created["log_event_id"]],
            "context": context_name,
            "entries": {"status": "active", "activated_by": "explicit"},
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
    assert "unclassified_fields" in str(response.json().get("detail"))

    log = (
        dbsession.query(LogEvent)
        .filter(
            LogEvent.project_id == project.id,
            LogEvent.id == created["log_event_id"],
        )
        .one()
    )
    assert "status" not in log.data
    assert "activated_by" not in log.data
    assert log.data["task_revision"] == 1


@pytest.mark.anyio
async def test_log_seam_rejects_authored_provider_event_update_via_typed_tasks_api(
    client: AsyncClient,
    assistant_id: int,
) -> None:
    created = await _create_provider_event_task(client, assistant_id=assistant_id)
    user_id = _auth_user_id()
    context_name = f"{user_id}/{assistant_id}/Tasks"
    trigger = dict(created["trigger"])
    trigger["trigger_config"] = {"repository": "octocat/other"}

    response = await client.put(
        "/v0/logs",
        json={
            "logs": [created["log_event_id"]],
            "context": context_name,
            "entries": {"trigger": trigger},
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
    assert (
        response.json()["detail"]
        == "provider_event_authored_update_use_typed_tasks_api"
    )
