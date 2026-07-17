"""Tests for POST /v0/tasks/{task_id}/cancel."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Context,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.tests.test_tasks.test_trigger_task import _auth_user_id, _seed_task
from orchestra.tests.utils import HEADERS


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls():
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


@pytest.fixture
async def assistant_id(client: AsyncClient) -> int:
    response = await client.post(
        "/v0/assistant",
        json={"first_name": "Cancel", "surname": "Task", "create_infra": False},
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    return int(response.json()["info"]["agent_id"])


@pytest.fixture
def mock_comms_job_stop():
    with patch(
        "orchestra.web.api.tasks.views._stop_comms_job",
        new_callable=AsyncMock,
    ) as mock_stop:
        mock_stop.return_value = True
        yield mock_stop


@pytest.fixture
def mock_task_cancel_event():
    with patch(
        "orchestra.web.api.tasks.views._emit_task_cancel_system_event",
        new_callable=AsyncMock,
    ) as mock_emit:
        yield mock_emit


def _seed_task_run(
    dbsession: Session,
    *,
    assistant_id: int,
    user_id: str,
    task_id: int,
    source_task_log_id: int,
    run_key: str = "offline:test:run-1",
    state: str = "running",
    job_name: str | None = "unity-task-run-abc123",
) -> LogEvent:
    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == user_id,
            Project.organization_id.is_(None),
            Project.name == "Assistants",
        )
        .one()
    )
    context_name = f"{user_id}/{assistant_id}/Tasks/Runs"
    context = (
        dbsession.query(Context)
        .filter(Context.project_id == project.id, Context.name == context_name)
        .one_or_none()
    )
    if context is None:
        context = Context(
            project_id=project.id,
            name=context_name,
            owner_scope="assistant",
            owner_id=assistant_id,
        )
        dbsession.add(context)
        dbsession.flush()
    data = {
        "run_key": run_key,
        "assistant_id": str(assistant_id),
        "task_id": task_id,
        "source_task_log_id": source_task_log_id,
        "state": state,
        "execution_mode": "offline",
    }
    if job_name is not None:
        data["job_name"] = job_name
    log = LogEvent(
        project_id=project.id,
        owner_key=f"a{assistant_id}",
        data=data,
    )
    dbsession.add(log)
    dbsession.flush()
    dbsession.add(
        LogEventContext(
            project_id=project.id,
            log_event_id=log.id,
            context_id=context.id,
            owner_key=f"a{assistant_id}",
        ),
    )
    dbsession.commit()
    dbsession.refresh(log)
    return log


@pytest.mark.anyio
async def test_cancel_task_marks_active_instance_cancelled(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_comms_job_stop: AsyncMock,
    mock_task_cancel_event: AsyncMock,
):
    task_row = _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=91,
        status_value="active",
        offline=True,
    )
    run_row = _seed_task_run(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=91,
        source_task_log_id=task_row.id,
        job_name="unity-task-run-cancel-me",
    )

    response = await client.post(
        "/v0/tasks/91/cancel",
        headers=HEADERS,
        json={"assistant_id": assistant_id, "reason": "operator cancel"},
    )
    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    info = response.json()["info"]
    assert info["task_id"] == 91
    assert info["assistant_id"] == assistant_id
    assert info["instance_id"] == 0
    assert info["status"] == "cancelled"
    assert info["run_key"] == "offline:test:run-1"
    assert info["job_name"] == "unity-task-run-cancel-me"
    assert info["job_stop_requested"] is True

    mock_comms_job_stop.assert_awaited_once_with(job_name="unity-task-run-cancel-me")
    mock_task_cancel_event.assert_not_awaited()

    dbsession.refresh(task_row)
    assert task_row.data["status"] == "cancelled"
    assert task_row.data["info"]["cancel_reason"] == "operator cancel"
    dbsession.refresh(run_row)
    assert run_row.data["state"] == "cancelled"
    assert run_row.data["result_summary"] == "operator cancel"


@pytest.mark.anyio
async def test_cancel_task_live_emits_system_event(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_comms_job_stop: AsyncMock,
    mock_task_cancel_event: AsyncMock,
):
    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=92,
        status_value="active",
        offline=False,
    )

    response = await client.post(
        "/v0/tasks/92/cancel",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )
    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    mock_comms_job_stop.assert_not_awaited()
    mock_task_cancel_event.assert_awaited_once()


@pytest.mark.anyio
async def test_cancel_task_returns_409_when_already_terminal(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
):
    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=93,
        status_value="completed",
    )

    response = await client.post(
        "/v0/tasks/93/cancel",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()


@pytest.mark.anyio
async def test_cancel_task_returns_404_when_missing(
    client: AsyncClient,
    assistant_id: int,
):
    response = await client.post(
        "/v0/tasks/404404/cancel",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND, response.json()
