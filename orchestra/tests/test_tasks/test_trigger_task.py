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
from orchestra.services.task_trigger_service import TaskTriggerTarget
from orchestra.tests.utils import HEADERS
from orchestra.web.api.tasks import views as task_views


@pytest.fixture
def mock_task_trigger_dispatch():
    with patch(
        "orchestra.web.api.tasks.views._dispatch_task_trigger",
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
    offline: bool = False,
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
            "offline": offline,
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


def _make_target(**overrides) -> TaskTriggerTarget:
    base = dict(
        assistant_id=42,
        task_id=17,
        source_task_log_id=9001,
        destination=None,
        task_name="Review report",
        task_description="Review the weekly report.",
        status="scheduled",
        instance_id=0,
        is_local=False,
        offline=False,
        activation_revision=None,
        entrypoint=None,
    )
    base.update(overrides)
    return TaskTriggerTarget(**base)


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

    response = await client.post(
        "/v0/tasks/17/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    assert response.json()["info"] == {
        "task_id": 17,
        "assistant_id": assistant_id,
        "status": "accepted",
    }
    mock_task_trigger_dispatch.assert_awaited_once()
    target = mock_task_trigger_dispatch.await_args.args[0]
    assert target.assistant_id == assistant_id
    assert target.task_id == 17
    assert target.source_task_log_id == task_row.id
    assert target.task_name == "Review report"
    assert target.offline is False


@pytest.mark.anyio
async def test_trigger_task_requires_assistant_id_body(client: AsyncClient):
    response = await client.post("/v0/tasks/17/trigger", headers=HEADERS)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_trigger_task_returns_404_when_task_missing(
    client: AsyncClient,
    assistant_id: int,
):
    response = await client.post(
        "/v0/tasks/999999/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

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

    response = await client.post(
        "/v0/tasks/19/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    mock_task_trigger_dispatch.assert_awaited_once()
    assert (
        mock_task_trigger_dispatch.await_args.args[0].source_task_log_id == task_row.id
    )


@pytest.mark.anyio
async def test_trigger_task_selects_requested_assistant_when_task_id_shared(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_task_trigger_dispatch: AsyncMock,
):
    second_response = await client.post(
        "/v0/assistant",
        json={"first_name": "Second", "surname": "Runner", "create_infra": False},
        headers=HEADERS,
    )
    assert second_response.status_code == status.HTTP_200_OK, second_response.json()
    second_assistant_id = int(second_response.json()["info"]["agent_id"])
    first_row = _seed_task(
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

    response = await client.post(
        "/v0/tasks/31/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    assert response.json()["info"]["assistant_id"] == assistant_id
    mock_task_trigger_dispatch.assert_awaited_once()
    target = mock_task_trigger_dispatch.await_args.args[0]
    assert target.assistant_id == assistant_id
    assert target.source_task_log_id == first_row.id
    assert target.task_name == "First task"


@pytest.mark.anyio
async def test_trigger_task_returns_404_for_wrong_assistant_id(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_task_trigger_dispatch: AsyncMock,
):
    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=41,
    )

    response = await client.post(
        "/v0/tasks/41/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id + 99999},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    mock_task_trigger_dispatch.assert_not_awaited()


@pytest.mark.anyio
async def test_dispatch_hosted_offline_posts_comms_explicit(monkeypatch):
    target = _make_target(offline=True, activation_revision="rev-abc", entrypoint=27)
    posted = {}

    class _FakeResponse:
        status_code = 200
        text = "ok"

    class _FakeClient:
        async def post(self, url, headers=None, json=None, timeout=None):
            posted["url"] = url
            posted["headers"] = headers
            posted["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(task_views, "COMMS_URL", "https://comms.test")
    monkeypatch.setattr(task_views, "ADMIN_KEY", "admin-key")
    monkeypatch.setattr(task_views, "get_async_client", lambda: _FakeClient())

    with patch.object(
        task_views,
        "_emit_task_trigger_system_event",
        new_callable=AsyncMock,
    ) as emit_event:
        request_id = await task_views._dispatch_task_trigger(target)

    assert request_id
    emit_event.assert_not_awaited()
    assert posted["url"] == (
        "https://comms.test/infra/task-activation/offline-dispatch"
    )
    assert posted["json"]["source_type"] == "explicit"
    assert posted["json"]["execution_mode"] == "offline"
    assert posted["json"]["activation_revision"] == "rev-abc"
    assert posted["json"]["entrypoint"] == 27
    assert posted["json"]["source_ref"] == request_id
    assert posted["headers"]["Authorization"] == "Bearer admin-key"


@pytest.mark.anyio
async def test_dispatch_hosted_live_emits_system_event_only():
    target = _make_target(offline=False)

    with (
        patch.object(
            task_views,
            "_dispatch_offline_task_to_comms",
            new_callable=AsyncMock,
        ) as offline_dispatch,
        patch.object(
            task_views,
            "_emit_task_trigger_system_event",
            new_callable=AsyncMock,
        ) as emit_event,
    ):
        await task_views._dispatch_task_trigger(target)

    offline_dispatch.assert_not_awaited()
    emit_event.assert_awaited_once()


@pytest.mark.anyio
async def test_dispatch_local_offline_emits_system_event():
    target = _make_target(offline=True, is_local=True, activation_revision="rev-local")

    with (
        patch.object(
            task_views,
            "_dispatch_offline_task_to_comms",
            new_callable=AsyncMock,
        ) as offline_dispatch,
        patch.object(
            task_views,
            "_emit_task_trigger_system_event",
            new_callable=AsyncMock,
        ) as emit_event,
    ):
        await task_views._dispatch_task_trigger(target)

    offline_dispatch.assert_not_awaited()
    emit_event.assert_awaited_once()


@pytest.mark.anyio
async def test_dispatch_hosted_offline_without_revision_raises():
    target = _make_target(offline=True, activation_revision=None)

    with pytest.raises(Exception) as exc_info:
        await task_views._dispatch_task_trigger(target)

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT


@pytest.mark.anyio
async def test_trigger_commits_session_before_dispatch(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
):
    """Outbound dispatch must not run while the resolve transaction is open."""

    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=51,
    )
    order: list[str] = []
    original_commit = dbsession.commit

    def _tracking_commit(*args, **kwargs):
        order.append("commit")
        return original_commit(*args, **kwargs)

    async def _tracking_dispatch(target):
        order.append("dispatch")
        assert target.task_id == 51

    dbsession.commit = _tracking_commit  # type: ignore[method-assign]
    try:
        with patch(
            "orchestra.web.api.tasks.views._dispatch_task_trigger",
            new=_tracking_dispatch,
        ):
            response = await client.post(
                "/v0/tasks/51/trigger",
                headers=HEADERS,
                json={"assistant_id": assistant_id},
            )
    finally:
        dbsession.commit = original_commit  # type: ignore[method-assign]

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    assert "commit" in order
    assert "dispatch" in order
    assert order.index("commit") < order.index("dispatch")
