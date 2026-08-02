import hashlib
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
from orchestra.services.task_machine_state_service import (
    get_open_task_execution,
    sync_task_executions_for_task_ids,
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
    with_schedule: bool = False,
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
    data = {
        "assistant_id": str(assistant_id),
        "task_id": task_id,
        "status": status_value,
        "name": name,
        "description": "Review the weekly report.",
        "offline": offline,
    }
    if with_schedule:
        data["schedule"] = {"start_at": "2026-07-20T09:00:00+00:00"}
        data["repeat"] = {"kind": "weekly", "weekday": "monday"}
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
    return log


def _auth_user_id() -> str:
    return str(os.getenv("AUTH_ACCOUNT_USER_ID"))


def _project_id_for_auth_user(dbsession: Session) -> int:
    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == _auth_user_id(),
            Project.organization_id.is_(None),
            Project.name == "Assistants",
        )
        .one()
    )
    return int(project.id)


def _project_scheduled_occurrence(
    dbsession: Session,
    *,
    assistant_id: int,
    task_id: int,
) -> dict:
    """Run the real projection and return the open scheduled execution it wrote."""

    project_id = _project_id_for_auth_user(dbsession)
    # Projection also asks Communication to materialize a delivery task; only
    # the Orchestra-side ledger row matters here.
    with patch(
        "orchestra.services.task_machine_state_service._post_task_execution_request",
    ):
        sync_task_executions_for_task_ids(
            dbsession,
            project_id,
            [task_id],
            tasks_context_name=f"{_auth_user_id()}/{assistant_id}/Tasks",
        )
    dbsession.commit()
    execution = get_open_task_execution(
        dbsession,
        project_id,
        assistant_id=str(assistant_id),
        task_id=task_id,
    )
    assert execution is not None, "projection wrote no open execution"
    return dict(execution.data)


def _capture_comms_dispatch(monkeypatch) -> dict:
    """Point offline dispatch at an in-memory Communication and record the post."""

    posted: dict = {}

    class _FakeResponse:
        status_code = 200
        text = "ok"

    class _FakeClient:
        async def post(self, url, headers=None, json=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(task_views, "COMMS_URL", "https://comms.test")
    monkeypatch.setattr(task_views, "ADMIN_KEY", "admin-key")
    monkeypatch.setattr(task_views, "get_async_client", lambda: _FakeClient())
    return posted


def _run_key_revision_digest(revision: str) -> str:
    """The revision fragment both run-key builders embed.

    Unify's ``build_offline_run_key`` is not importable here, so pin the one
    part of the key a revision controls. Two runs whose digests differ cannot
    share a key under either builder.
    """

    return hashlib.sha256(str(revision or "").encode("utf-8")).hexdigest()[:12]


def _make_target(**overrides) -> TaskTriggerTarget:
    base = dict(
        assistant_id=42,
        task_id=17,
        source_task_log_id=9001,
        destination=None,
        task_name="Review report",
        task_description="Review the weekly report.",
        status="scheduled",
        is_local=False,
        offline=False,
        enabled=True,
        revision=None,
        entrypoint=None,
    )
    base.update(overrides)
    return TaskTriggerTarget(**base)


def test_select_current_target_prefers_enabled_team_row():
    from orchestra.services.task_trigger_service import _select_current_target

    personal = _make_target(
        source_task_log_id=1,
        destination=None,
        enabled=False,
        offline=True,
    )
    team = _make_target(
        source_task_log_id=2,
        destination="team:11",
        enabled=True,
        offline=True,
    )
    assert _select_current_target([personal, team]) is team


def test_select_current_target_prefers_newer_definition_when_tied():
    from orchestra.services.task_trigger_service import _select_current_target

    older = _make_target(
        source_task_log_id=10,
        destination="team:11",
        offline=True,
    )
    newer = _make_target(
        source_task_log_id=20,
        destination="team:11",
        offline=True,
    )
    assert _select_current_target([older, newer]) is newer


@pytest.mark.anyio
async def test_trigger_task_dispatches_definition_row(
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
        with_schedule=True,
    )

    response = await client.post(
        "/v0/tasks/17/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    info = response.json()["info"]
    assert info == {
        "task_id": 17,
        "assistant_id": assistant_id,
        "source_task_log_id": task_row.id,
        "status": "accepted",
    }
    mock_task_trigger_dispatch.assert_awaited_once()
    target = mock_task_trigger_dispatch.await_args.args[0]
    assert target.assistant_id == assistant_id
    assert target.task_id == 17
    assert target.source_task_log_id == task_row.id
    assert target.task_name == "Review report"
    assert target.offline is False

    original = dbsession.query(LogEvent).filter(LogEvent.id == task_row.id).one()
    assert original.data["schedule"]["start_at"] == "2026-07-20T09:00:00+00:00"
    # Trigger must not fork a second Tasks definition row (no instance clone).
    tasks_context = f"{_auth_user_id()}/{assistant_id}/Tasks"
    definition_count = (
        dbsession.query(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .filter(
            LogEvent.project_id == task_row.project_id,
            Context.name == tasks_context,
            LogEvent.data.op("->>")("task_id") == "17",
        )
        .count()
    )
    assert definition_count == 1


@pytest.mark.anyio
async def test_trigger_task_repeated_calls_use_same_definition(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_task_trigger_dispatch: AsyncMock,
):
    task_row = _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=28,
        with_schedule=True,
    )
    first = await client.post(
        "/v0/tasks/28/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )
    second = await client.post(
        "/v0/tasks/28/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )
    assert first.status_code == status.HTTP_202_ACCEPTED, first.json()
    assert second.status_code == status.HTTP_202_ACCEPTED, second.json()
    assert first.json()["info"]["source_task_log_id"] == task_row.id
    assert second.json()["info"]["source_task_log_id"] == task_row.id
    tasks_context = f"{_auth_user_id()}/{assistant_id}/Tasks"
    definition_count = (
        dbsession.query(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .filter(
            LogEvent.project_id == task_row.project_id,
            Context.name == tasks_context,
            LogEvent.data.op("->>")("task_id") == "28",
        )
        .count()
    )
    assert definition_count == 1


@pytest.mark.anyio
async def test_update_logs_refuses_instance_id_mutation_on_legacy_tasks_context(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
):
    """Guard still fires on pre-migration contexts that auto-count instance_id.

    Current Tasks contexts are keyed by ``task_id`` alone, so the reshaping
    below is deliberately the *legacy* schema — it reproduces the contexts that
    predate the collapse onto Tasks definitions plus ``Tasks/Executions``.
    """
    task_row = _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=32,
    )
    project = dbsession.query(Project).filter(Project.id == task_row.project_id).one()
    context = (
        dbsession.query(Context)
        .filter(
            Context.project_id == project.id,
            Context.name == f"{_auth_user_id()}/{assistant_id}/Tasks",
        )
        .one()
    )
    context.auto_counting = {"task_id": None, "instance_id": "task_id"}
    context.unique_key_names = ["task_id", "instance_id"]
    context.unique_key_types = ["int", "int"]
    dbsession.commit()

    response = await client.put(
        "/v0/logs",
        headers=HEADERS,
        json={
            "project": "Assistants",
            "context": context.name,
            "logs": [task_row.id],
            "entries": {"instance_id": 99, "status": "failed"},
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
    assert "instance_id" in str(response.json().get("detail", "")).lower()
    dbsession.refresh(task_row)
    assert "instance_id" not in task_row.data or task_row.data.get("instance_id") in (
        None,
        0,
    )


@pytest.mark.anyio
async def test_trigger_task_terminal_definition_returns_409(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    mock_task_trigger_dispatch: AsyncMock,
):
    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=21,
        status_value="completed",
    )

    response = await client.post(
        "/v0/tasks/21/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "not runnable" in response.json()["detail"]
    mock_task_trigger_dispatch.assert_not_awaited()


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
    target = mock_task_trigger_dispatch.await_args.args[0]
    assert target.source_task_log_id == task_row.id


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
    target = _make_target(offline=True, revision="rev-abc", entrypoint=27)
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
    assert posted["url"] == ("https://comms.test/infra/task-execution/offline-dispatch")
    assert posted["json"]["wake"] == "explicit"
    assert posted["json"]["delivery"] == "offline"
    assert posted["json"]["revision"] == "rev-abc"
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
    target = _make_target(offline=True, is_local=True, revision="rev-local")

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
    """Resolution always mints one, so reaching this guard is a server fault.

    It stays because an empty revision digests to the same twelve characters
    for every task, which would collide run keys across the whole fleet.
    """

    target = _make_target(offline=True, revision=None)

    with pytest.raises(Exception) as exc_info:
        await task_views._dispatch_task_trigger(target)

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


@pytest.mark.anyio
async def test_trigger_offline_task_dispatches_with_no_open_execution(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    monkeypatch,
):
    """An on-demand run is most needed when the scheduler has queued nothing.

    Sourcing the revision from an open scheduled execution made the trigger
    fail exactly then, so the only way to run a task by hand was to wait for
    its schedule to come round.
    """

    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=61,
        offline=True,
        with_schedule=True,
    )
    posted = _capture_comms_dispatch(monkeypatch)
    assert (
        get_open_task_execution(
            dbsession,
            _project_id_for_auth_user(dbsession),
            assistant_id=str(assistant_id),
            task_id=61,
        )
        is None
    )

    response = await client.post(
        "/v0/tasks/61/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    assert posted["url"] == "https://comms.test/infra/task-execution/offline-dispatch"
    assert posted["json"]["wake"] == "explicit"
    assert posted["json"]["delivery"] == "offline"
    assert posted["json"]["revision"]


@pytest.mark.anyio
async def test_trigger_offline_task_mints_its_own_occurrence(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    monkeypatch,
):
    """A queued occurrence is the scheduler's; the trigger names its own.

    Borrowing the scheduled revision declared an ``explicit`` wake under a
    fingerprint computed for a ``scheduled`` one at a future slot, and left a
    manual run's identity hostage to whatever the scheduler had pending.
    """

    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=62,
        offline=True,
        with_schedule=True,
    )
    scheduled = _project_scheduled_occurrence(
        dbsession,
        assistant_id=assistant_id,
        task_id=62,
    )
    assert scheduled["wake"] == "scheduled"
    assert scheduled["revision"]
    posted = _capture_comms_dispatch(monkeypatch)

    response = await client.post(
        "/v0/tasks/62/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    triggered_revision = posted["json"]["revision"]
    assert triggered_revision
    assert triggered_revision != scheduled["revision"]
    # Neither builder can produce one key from two revisions, so the triggered
    # run creates its own Execution rather than adopting the queued one.
    assert _run_key_revision_digest(triggered_revision) != _run_key_revision_digest(
        scheduled["revision"],
    )
    assert _run_key_revision_digest(triggered_revision) not in scheduled["run_key"]
    assert scheduled["run_key"].startswith("offline:scheduled:")


@pytest.mark.anyio
async def test_trigger_leaves_the_scheduled_occurrence_armed(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    monkeypatch,
):
    """The queued occurrence survives an on-demand run untouched."""

    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=63,
        offline=True,
        with_schedule=True,
    )
    before = _project_scheduled_occurrence(
        dbsession,
        assistant_id=assistant_id,
        task_id=63,
    )
    _capture_comms_dispatch(monkeypatch)

    response = await client.post(
        "/v0/tasks/63/trigger",
        headers=HEADERS,
        json={"assistant_id": assistant_id},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    dbsession.expire_all()
    after = get_open_task_execution(
        dbsession,
        _project_id_for_auth_user(dbsession),
        assistant_id=str(assistant_id),
        task_id=63,
    )
    assert after is not None
    assert after.data["run_key"] == before["run_key"]
    assert after.data["revision"] == before["revision"]
    assert after.data["state"] == before["state"]


@pytest.mark.anyio
async def test_trigger_closes_session_before_dispatch(
    client: AsyncClient,
    dbsession: Session,
    assistant_id: int,
    fastapi_app,
):
    """Resolve must commit and close its DB session before outbound dispatch."""

    _seed_task(
        dbsession,
        assistant_id=assistant_id,
        user_id=_auth_user_id(),
        task_id=51,
    )
    order: list[str] = []
    real_factory = fastapi_app.state.db_session_factory

    def _tracking_factory(*args, **kwargs):
        session = real_factory(*args, **kwargs)
        original_commit = session.commit
        original_close = session.close

        def _commit(*cargs, **ckwargs):
            order.append("commit")
            return original_commit(*cargs, **ckwargs)

        def _close(*cargs, **ckwargs):
            order.append("close")
            return original_close(*cargs, **ckwargs)

        session.commit = _commit  # type: ignore[method-assign]
        session.close = _close  # type: ignore[method-assign]
        return session

    async def _tracking_dispatch(target):
        order.append("dispatch")
        assert target.task_id == 51
        assert "close" in order

    fastapi_app.state.db_session_factory = _tracking_factory
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
        fastapi_app.state.db_session_factory = real_factory

    assert response.status_code == status.HTTP_202_ACCEPTED, response.json()
    assert order.index("commit") < order.index("close") < order.index("dispatch")


def test_derive_tasks_context_name_uses_owner_team_for_team_owned(monkeypatch):
    from types import SimpleNamespace

    from orchestra.services import task_machine_state_service as tms

    assistant = SimpleNamespace(owner_team_id=11, user_id="user-1")
    monkeypatch.setattr(
        tms,
        "_get_assistant_for_task_machine_lookup",
        lambda **kwargs: assistant,
    )
    assert (
        tms._derive_tasks_context_name_from_assistant(
            session=SimpleNamespace(),
            project_id=1,
            assistant_id="1406",
        )
        == "Teams/11/Tasks"
    )


def test_derive_tasks_context_name_uses_personal_when_not_team_owned(monkeypatch):
    from types import SimpleNamespace

    from orchestra.services import task_machine_state_service as tms

    assistant = SimpleNamespace(owner_team_id=None, user_id="user-1")
    monkeypatch.setattr(
        tms,
        "_get_assistant_for_task_machine_lookup",
        lambda **kwargs: assistant,
    )
    assert (
        tms._derive_tasks_context_name_from_assistant(
            session=SimpleNamespace(),
            project_id=1,
            assistant_id="1406",
        )
        == "user-1/1406/Tasks"
    )
