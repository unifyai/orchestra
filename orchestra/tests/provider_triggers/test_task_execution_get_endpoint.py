"""Ownership-scoped POST /v0/task-execution/get for Unity live provider-event dispatch."""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.core_models import Project
from orchestra.db.models.orchestra_models import Assistant
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    create_task_run_if_absent,
)
from orchestra.tests.test_log import HEADERS

PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
TASK_RUN_GET_PATH = "/v0/task-execution/get"


def _seed_provider_event_run(dbsession: Session) -> tuple[Assistant, dict]:
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="RunGet", surname="Bot")
    dbsession.add(assistant)
    dbsession.flush()

    project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
    dbsession.add(project)
    dbsession.flush()

    task_id = 5151
    run_key = (
        f"live:provider_event:{assistant.agent_id}:{task_id}:"
        f"binding-get:revdigest:{'b' * 64}"
    )
    run, created = create_task_run_if_absent(
        dbsession,
        project.id,
        {
            "run_key": run_key,
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "wake": "provider_event",
            "delivery": "live",
            "state": "pending",
        },
    )
    assert created is True
    dbsession.commit()
    return assistant, dict(run.data or {})


@pytest.mark.anyio
async def test_task_run_get_returns_owned_provider_event_run(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    assistant, seeded = _seed_provider_event_run(dbsession)
    response = await client.post(
        TASK_RUN_GET_PATH,
        headers=HEADERS,
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "run_key": seeded["run_key"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    run = body["run"]
    assert run is not None
    assert run["run_key"] == seeded["run_key"]
    assert int(run["task_id"]) == 5151
    assert run["wake"] == "provider_event"
    assert run["delivery"] == "live"
    assert int(run["run_id"]) == int(seeded["run_id"])


@pytest.mark.anyio
async def test_task_run_get_missing_run_returns_null(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Missing", surname="Run")
    dbsession.add(assistant)
    dbsession.flush()
    project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
    dbsession.add(project)
    dbsession.commit()

    response = await client.post(
        TASK_RUN_GET_PATH,
        headers=HEADERS,
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "run_key": "live:provider_event:missing:1:binding:rev:identity",
        },
    )
    assert response.status_code == 200
    assert response.json()["run"] is None
