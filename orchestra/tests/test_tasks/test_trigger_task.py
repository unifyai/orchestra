import os
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
from orchestra.tests.utils import HEADERS


@pytest.fixture(autouse=True)
def mock_task_trigger_dispatch():
    with patch(
        "orchestra.web.api.tasks.views._dispatch_task_trigger_to_adapters",
        new_callable=AsyncMock,
    ) as mock_dispatch:
        yield mock_dispatch


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
        json={"first_name": "Task", "surname": "Runner", "create_infra": False},
        headers=HEADERS,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    return int(response.json()["info"]["agent_id"])


def _seed_task(
    dbsession: Session,
    *,
    assistant_id: int,
    user_id: str,
    task_id: int,
    status_value: str = "scheduled",
    name: str = "Review report",
    legacy_context_owner: bool = False,
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
    context_name = f"{user_id}/{assistant_id}/Tasks"
    context = (
        dbsession.query(Context)
        .filter(Context.project_id == project.id, Context.name == context_name)
        .one_or_none()
    )
    if context is None:
        context_kwargs = {"project_id": project.id, "name": context_name}
        if not legacy_context_owner:
            context_kwargs.update(owner_scope="assistant", owner_id=assistant_id)
        context = Context(**context_kwargs)
        dbsession.add(context)
        dbsession.flush()
    log = LogEvent(
        project_id=project.id,
        owner_key=f"a{assistant_id}",
        data={
            "assistant_id": str(assistant_id),
            "task_id": task_id,
            "instance_id": 0,
            "status": status_value,
            "name": name,
            "description": "Review the weekly report.",
        },
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
    dbsession.flush()
    return log


def _auth_user_id() -> str:
    return str(os.getenv("AUTH_ACCOUNT_USER_ID"))


@pytest.mark.anyio
async def test_trigger_task_dispatches_to_adapters(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_task_trigger_dispatch: AsyncMock,
):
    task_row = _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=17,
    )

    response = await client.post("/v0/tasks/17/trigger", headers=HEADERS)

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    assert response.json()["info"] == {
        "task_id": 17,
        "assistant_id": assistant_id,
        "status": "accepted",
    }
    mock_task_trigger_dispatch.assert_awaited_once_with(
        assistant_id=assistant_id,
        task_id=17,
        source_task_log_id=task_row.id,
        task_label="Review report",
        task_summary="Review the weekly report.",
        is_local=False,
    )


@pytest.mark.anyio
async def test_trigger_task_returns_404_when_task_missing(client: AsyncClient):
    response = await client.post("/v0/tasks/999999/trigger", headers=HEADERS)

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_trigger_task_accepts_legacy_assistant_context_without_owner_metadata(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_task_trigger_dispatch: AsyncMock,
):
    task_row = _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=19,
        legacy_context_owner=True,
    )

    response = await client.post("/v0/tasks/19/trigger", headers=HEADERS)

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    mock_task_trigger_dispatch.assert_awaited_once()
    assert mock_task_trigger_dispatch.await_args.kwargs["source_task_log_id"] == task_row.id


@pytest.mark.anyio
async def test_trigger_task_rejects_ambiguous_task_id(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
):
    second_response = await client.post(
        "/v0/assistant",
        json={"first_name": "Second", "surname": "Runner", "create_infra": False},
        headers=HEADERS,
    )
    assert second_response.status_code == status.HTTP_200_OK, second_response.json()
    second_assistant_id = int(second_response.json()["info"]["agent_id"])
    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=31,
        name="First task",
    )
    _seed_task(
        dbsession,
        assistant_id=second_assistant_id,
        user_id=_auth_user_id(),
        task_id=31,
        name="Second task",
    )

    response = await client.post("/v0/tasks/31/trigger", headers=HEADERS)

    assert response.status_code == status.HTTP_409_CONFLICT
