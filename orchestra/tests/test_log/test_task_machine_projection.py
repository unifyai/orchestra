"""Integration tests for Unity task machine-state projection."""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    Organization,
    Team,
    TeamAssistantMembership,
)
from orchestra.services import task_machine_state_service
from orchestra.tests.test_log import (
    HEADERS,
    HEADERS_2,
    _create_log,
    _create_project,
    _delete_logs,
    _update_logs,
)
from orchestra.tests.utils import ADMIN_HEADERS

TASKS_CONTEXT = "1/42/Tasks"
TASK_MACHINE_PROJECT_NAME = task_machine_state_service.TASK_MACHINE_PROJECT_NAME
TASK_EXECUTIONS_CONTEXT = task_machine_state_service.build_task_executions_context_name(
    TASKS_CONTEXT,
)
TASK_OUTBOUND_OPERATIONS_CONTEXT = (
    task_machine_state_service.build_task_outbound_operations_context_name(
        TASKS_CONTEXT,
    )
)
PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
SECONDARY_USER_ID = "seconday_user"
_ORIGINAL_RECONCILE_SCHEDULED_EXECUTION_MATERIALIZATION = (
    task_machine_state_service._reconcile_scheduled_execution_materialization
)


async def _ensure_task_machine_project(client: AsyncClient) -> None:
    """Create the task machine project when it does not already exist."""

    response = await _create_project(client, TASK_MACHINE_PROJECT_NAME)
    assert response.status_code in (200, 400), response.json()


async def _get_context_logs(
    client: AsyncClient,
    *,
    context_name: str,
    user: int = 1,
) -> list[dict]:
    """Fetch logs from one task-machine context and return the payload list."""

    headers = HEADERS if user == 1 else HEADERS_2
    response = await client.get(
        "/v0/logs",
        params={"project_name": TASK_MACHINE_PROJECT_NAME, "context": context_name},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    return response.json()["logs"]


def _assistant_tasks_context(*, user_id: str, assistant_id: int) -> str:
    """Return the assistant-scoped Tasks context for one seeded owner."""

    return f"{user_id}/{assistant_id}/Tasks"


def _assistant_scoped_scheduled_entries(
    *,
    user_id: str,
    assistant_id: int,
    task_id: int,
) -> dict:
    """Return a scheduled task row bound to one explicit assistant scope."""

    entries = _scheduled_task_entries(task_id=task_id)
    entries["_user_id"] = user_id
    entries["_assistant_id"] = str(assistant_id)
    return entries


def _make_assistant(dbsession, *, user_id: str) -> Assistant:
    """Create a minimal assistant row for task-machine admin lookup tests."""

    assistant = Assistant(
        user_id=user_id,
        first_name="Task",
        surname="Admin",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _ensure_organization(dbsession, *, owner_user_id: str) -> Organization:
    """Return or create an organization for task routing tests."""

    org = (
        dbsession.query(Organization)
        .filter(Organization.owner_id == owner_user_id)
        .first()
    )
    if org is None:
        org = Organization(name="Task Routing Org", owner_id=owner_user_id)
        dbsession.add(org)
        dbsession.flush()
    return org


def _make_team_member(
    dbsession,
    *,
    assistant: Assistant,
    owner_user_id: str = PRIMARY_USER_ID,
) -> Team:
    """Create a shared team and attach the assistant as a live member."""

    org = _ensure_organization(dbsession, owner_user_id=owner_user_id)
    team = Team(
        name="Project Room",
        description="Project room workspace for task routing tests.",
        organization_id=org.id,
        status="active",
    )
    dbsession.add(team)
    dbsession.flush()
    dbsession.add(
        TeamAssistantMembership(
            assistant_id=assistant.agent_id,
            team_id=team.id,
            added_by=owner_user_id,
        ),
    )
    dbsession.flush()
    return team


@pytest.fixture(autouse=True)
def materialization_calls(monkeypatch):
    """Capture scheduled activation sync requests without hitting Communication."""

    calls: list[tuple[dict | None, dict | None]] = []

    def _capture(*, previous_execution, current_execution):
        calls.append((previous_execution, current_execution))

    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_execution_materialization",
        _capture,
    )
    return calls


def _scheduled_task_entries(
    *,
    task_id: int,
    instance_id: int = 0,
    status: str = "scheduled",
    start_at: str = "2026-04-10T09:00:00+00:00",
) -> dict:
    """Return a minimal scheduled task row."""

    return {
        "task_id": task_id,
        "instance_id": instance_id,
        "status": status,
        "_user_id": "1",
        "_assistant_id": "42",
        "schedule": {
            "start_at": start_at,
        },
        "repeat": [{"unit": "day", "count": 1}],
    }


def _trigger_task_entries(
    *,
    task_id: int,
    instance_id: int = 0,
    status: str = "triggerable",
    medium: str = "email",
) -> dict:
    """Return a minimal triggerable task row."""

    return {
        "task_id": task_id,
        "instance_id": instance_id,
        "status": status,
        "_user_id": "1",
        "_assistant_id": "42",
        "trigger": {
            "medium": medium,
            "from_contact_ids": [17],
            "omit_contact_ids": [18],
            "interrupt": True,
            "recurring": True,
        },
    }


def _offline_task_entries(
    *,
    task_id: int,
    entrypoint: int | None,
) -> dict:
    """Return a minimal offline scheduled task row."""

    entries = _scheduled_task_entries(task_id=task_id)
    entries["offline"] = True
    if entrypoint is not None:
        entries["entrypoint"] = entrypoint
    return entries


def test_scheduled_execution_upsert_body_includes_wake_context():
    """Scheduled activation sync should carry compact human-facing wake context."""

    body = task_machine_state_service._scheduled_execution_upsert_body(
        {
            "assistant_id": "42",
            "task_id": 101,
            "source_task_log_id": 555,
            "wake": "scheduled",
            "delivery": "live",
            "revision": "rev-1",
            "scheduled_for": "2026-04-10T09:00:00+00:00",
            "task_name": "Morning briefing",
            "task_description": (
                "Prepare the morning update before the user checks in."
            ),
            "repeat": [{"unit": "day", "count": 1}],
        },
    )

    assert body is not None
    assert body["task_label"] == "Morning briefing"
    assert (
        body["task_summary"] == "Prepare the morning update before the user checks in."
    )
    assert body["visibility_policy"] == "silent_by_default"
    assert body["recurrence_hint"] == "recurring"


def _scheduled_execution_payload(
    *,
    revision: str = "rev-1",
    next_due_at: str = "2026-04-10T09:00:00+00:00",
    delivery: str = "offline",
    source_task_log_id: int = 555,
) -> dict:
    return {
        "assistant_id": "42",
        "task_id": 101,
        "source_task_log_id": source_task_log_id,
        "wake": "scheduled",
        "delivery": delivery,
        "revision": revision,
        "scheduled_for": next_due_at,
        "task_name": "Morning briefing",
        "task_description": "Prepare the morning update before the user checks in.",
    }


def test_reconcile_skips_unchanged_scheduled_delivery_identity(monkeypatch):
    """Repeated projection of the same delivery must not rematerialize."""

    posts = []
    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_execution_materialization",
        _ORIGINAL_RECONCILE_SCHEDULED_EXECUTION_MATERIALIZATION,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "_post_task_execution_request",
        lambda **kwargs: posts.append(kwargs),
    )

    previous = _scheduled_execution_payload(source_task_log_id=555)
    current = {**_scheduled_execution_payload(source_task_log_id=555)}
    current["last_materialized_at"] = "2026-04-10T08:00:00+00:00"

    task_machine_state_service._reconcile_scheduled_execution_materialization(
        previous_execution=previous,
        current_execution=current,
    )

    assert posts == []


def test_reconcile_upserts_changed_scheduled_delivery_identity(monkeypatch):
    """Changed due time should rematerialize and include stale cleanup fields."""

    posts = []
    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_execution_materialization",
        _ORIGINAL_RECONCILE_SCHEDULED_EXECUTION_MATERIALIZATION,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "_post_task_execution_request",
        lambda **kwargs: posts.append(kwargs),
    )

    task_machine_state_service._reconcile_scheduled_execution_materialization(
        previous_execution=_scheduled_execution_payload(
            next_due_at="2026-04-10T09:00:00+00:00",
        ),
        current_execution=_scheduled_execution_payload(
            revision="rev-2",
            next_due_at="2026-04-10T09:30:00+00:00",
        ),
    )

    assert len(posts) == 1
    assert posts[0]["path"] == task_machine_state_service._TASK_EXECUTION_UPSERT_PATH
    body = posts[0]["body"]
    assert body["revision"] == "rev-2"
    assert body["scheduled_for"] == "2026-04-10T09:30:00+00:00"
    assert body["previous_revision"] == "rev-1"
    assert body["previous_scheduled_for"] == "2026-04-10T09:00:00+00:00"


def test_reconcile_deletes_unarmed_scheduled_delivery(monkeypatch):
    """Dropping an armed activation should still delete its Cloud Task."""

    posts = []
    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_execution_materialization",
        _ORIGINAL_RECONCILE_SCHEDULED_EXECUTION_MATERIALIZATION,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "_post_task_execution_request",
        lambda **kwargs: posts.append(kwargs),
    )

    task_machine_state_service._reconcile_scheduled_execution_materialization(
        previous_execution=_scheduled_execution_payload(),
        current_execution=None,
    )

    assert len(posts) == 1
    assert posts[0]["path"] == task_machine_state_service._TASK_EXECUTION_DELETE_PATH
    assert posts[0]["body"]["revision"] == "rev-1"


def test_post_task_execution_request_skips_in_self_host_mode(monkeypatch):
    """Self-host uses Unity's LocalActivationScheduler instead of Communication."""

    posts: list[tuple] = []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *args, **kwargs):
            posts.append((args, kwargs))

    monkeypatch.setenv("SELF_HOST", "1")
    monkeypatch.setenv("UNITY_COMMS_URL", "http://comms.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(task_machine_state_service.httpx, "Client", _FakeClient)

    task_machine_state_service._post_task_execution_request(
        path=task_machine_state_service._TASK_EXECUTION_UPSERT_PATH,
        body={"assistant_id": "42", "task_id": 101},
    )

    assert posts == []


def test_post_task_execution_request_posts_when_not_self_host(monkeypatch):
    """Hosted deployments still mirror scheduled activations into Communication."""

    posts: list[tuple] = []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return SimpleNamespace(
                status_code=200,
                text='{"success": true}',
                raise_for_status=lambda: None,
            )

    monkeypatch.delenv("SELF_HOST", raising=False)
    monkeypatch.setenv("UNITY_COMMS_URL", "http://comms.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(task_machine_state_service.httpx, "Client", _FakeClient)

    task_machine_state_service._post_task_execution_request(
        path=task_machine_state_service._TASK_EXECUTION_UPSERT_PATH,
        body={"assistant_id": "42", "task_id": 101},
    )

    assert len(posts) == 1
    url, kwargs = posts[0]
    assert url == "http://comms.test/infra/task-execution/upsert"
    assert kwargs["json"] == {"assistant_id": "42", "task_id": 101}
    assert kwargs["headers"]["Authorization"] == "Bearer test-admin-key"


@pytest.mark.anyio
async def test_task_create_projects_scheduled_execution(
    client: AsyncClient,
    materialization_calls,
):
    """Creating a scheduled task should materialize one activation row."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=101),
    )
    assert response.status_code == 200, response.json()
    created_task_log_id = response.json()["log_event_ids"][0]

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    assert len(executions) == 1
    execution = executions[0]["entries"]
    assert execution["assistant_id"] == "42"
    assert execution["task_id"] == 101
    assert execution["source_task_log_id"] == created_task_log_id
    assert execution["wake"] == "scheduled"
    assert execution["delivery"] == "live"
    assert execution["entrypoint"] is None
    assert execution["scheduled_for"] == "2026-04-10T09:00:00+00:00"
    assert execution["repeat"] == [{"unit": "day", "count": 1}]
    assert execution["revision"]
    assert materialization_calls == [(None, execution)]


@pytest.mark.anyio
async def test_team_task_projects_execution_into_executor_context(
    client: AsyncClient,
    dbsession,
    materialization_calls,
):
    """Shared task definitions should create executor-owned activation rows."""

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    team = _make_team_member(dbsession, assistant=assistant)
    team_tasks_context = f"Teams/{team.id}/Tasks"
    executor_execution_context = (
        task_machine_state_service.build_task_executions_context_name(
            _assistant_tasks_context(
                user_id=PRIMARY_USER_ID,
                assistant_id=assistant.agent_id,
            ),
        )
    )
    entries = _assistant_scoped_scheduled_entries(
        user_id=PRIMARY_USER_ID,
        assistant_id=assistant.agent_id,
        task_id=111,
    )
    entries["assistant_id"] = str(assistant.agent_id)

    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=team_tasks_context,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    executions = await _get_context_logs(
        client,
        context_name=executor_execution_context,
    )
    matching = [
        log["entries"] for log in executions if log["entries"]["task_id"] == 111
    ]
    assert len(matching) == 1
    execution = matching[0]
    assert execution["assistant_id"] == str(assistant.agent_id)
    assert execution["destination"] == f"team:{team.id}"
    assert materialization_calls == [(None, execution)]

    shared_execution_context = f"Teams/{team.id}/Tasks/Executions"
    assert (
        dbsession.query(Context)
        .filter(Context.name == shared_execution_context)
        .one_or_none()
        is None
    )


@pytest.mark.anyio
async def test_deleting_team_task_removes_executor_execution_by_destination(
    client: AsyncClient,
    dbsession,
    materialization_calls,
):
    """Deleting a shared team task removes its executor-owned activation rows.

    Drives ``_delete_open_executions_by_task_destination`` (the team-surface
    resync-delete path), so the partition-prune guard verifies its log_event
    scan stays pruned to the owning project.
    """

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    team = _make_team_member(dbsession, assistant=assistant)
    team_tasks_context = f"Teams/{team.id}/Tasks"
    executor_execution_context = (
        task_machine_state_service.build_task_executions_context_name(
            _assistant_tasks_context(
                user_id=PRIMARY_USER_ID,
                assistant_id=assistant.agent_id,
            ),
        )
    )
    entries = _assistant_scoped_scheduled_entries(
        user_id=PRIMARY_USER_ID,
        assistant_id=assistant.agent_id,
        task_id=131,
    )
    entries["assistant_id"] = str(assistant.agent_id)

    create = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=team_tasks_context,
        entries=entries,
    )
    assert create.status_code == 200, create.json()
    team_task_log_id = create.json()["log_event_ids"][0]

    executions = await _get_context_logs(
        client,
        context_name=executor_execution_context,
    )
    assert any(log["entries"]["task_id"] == 131 for log in executions)

    delete = await _delete_logs(
        client,
        [(team_task_log_id, None)],
        project_name=TASK_MACHINE_PROJECT_NAME,
        context=team_tasks_context,
    )
    assert delete.status_code == 200, delete.json()

    executions_after = await _get_context_logs(
        client,
        context_name=executor_execution_context,
    )
    assert all(log["entries"]["task_id"] != 131 for log in executions_after)


@pytest.mark.anyio
async def test_team_task_membership_mismatch_does_not_project_execution(
    client: AsyncClient,
    dbsession,
    materialization_calls,
):
    """Shared task rows should not arm assistants that no longer belong to the team."""

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    org = _ensure_organization(dbsession, owner_user_id=PRIMARY_USER_ID)
    team = Team(
        name="Restricted Room",
        description="Restricted room workspace for revoked membership tests.",
        organization_id=org.id,
        status="active",
    )
    dbsession.add(team)
    dbsession.flush()
    entries = _assistant_scoped_scheduled_entries(
        user_id=PRIMARY_USER_ID,
        assistant_id=assistant.agent_id,
        task_id=112,
    )
    entries["assistant_id"] = str(assistant.agent_id)

    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=f"Teams/{team.id}/Tasks",
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    executor_execution_context = (
        task_machine_state_service.build_task_executions_context_name(
            _assistant_tasks_context(
                user_id=PRIMARY_USER_ID,
                assistant_id=assistant.agent_id,
            ),
        )
    )
    executions = await _get_context_logs(
        client,
        context_name=executor_execution_context,
    )
    assert all(log["entries"]["task_id"] != 112 for log in executions)
    assert materialization_calls == []


@pytest.mark.anyio
async def test_deleting_team_does_not_project_execution(
    client: AsyncClient,
    dbsession,
    materialization_calls,
):
    """Deleting teams stop arming new scheduled work for member assistants."""

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    team = _make_team_member(dbsession, assistant=assistant)
    team.status = "deleting"
    dbsession.flush()
    entries = _assistant_scoped_scheduled_entries(
        user_id=PRIMARY_USER_ID,
        assistant_id=assistant.agent_id,
        task_id=113,
    )
    entries["assistant_id"] = str(assistant.agent_id)

    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=f"Teams/{team.id}/Tasks",
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    executor_execution_context = (
        task_machine_state_service.build_task_executions_context_name(
            _assistant_tasks_context(
                user_id=PRIMARY_USER_ID,
                assistant_id=assistant.agent_id,
            ),
        )
    )
    executions = await _get_context_logs(
        client,
        context_name=executor_execution_context,
    )
    assert all(log["entries"]["task_id"] != 113 for log in executions)
    assert materialization_calls == []


@pytest.mark.anyio
async def test_task_update_reconciles_new_schedule_head(
    client: AsyncClient,
    materialization_calls,
):
    """Schedule edits should carry both the old and new queue-head due times."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=151),
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]
    materialization_calls.clear()

    response = await _update_logs(
        client,
        [log_id],
        _scheduled_task_entries(
            task_id=151,
            start_at="2026-04-10T11:30:00+00:00",
        ),
        context=TASKS_CONTEXT,
        overwrite=True,
    )
    assert response.status_code == 200, response.json()
    assert len(materialization_calls) == 1
    previous_execution, current_execution = materialization_calls[0]
    assert previous_execution["scheduled_for"] == "2026-04-10T09:00:00+00:00"
    assert current_execution["scheduled_for"] == "2026-04-10T11:30:00+00:00"


@pytest.mark.anyio
async def test_disabled_scheduled_task_does_not_project_execution(
    client: AsyncClient,
):
    """enabled=False scheduled tasks must not arm Tasks/Executions."""

    await _ensure_task_machine_project(client)
    entries = _scheduled_task_entries(task_id=260)
    entries["enabled"] = False
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    assert all(log["entries"]["task_id"] != 260 for log in executions)


@pytest.mark.anyio
async def test_disabled_trigger_task_does_not_project_execution(
    client: AsyncClient,
):
    """enabled=False triggerable tasks must not arm Tasks/Executions."""

    await _ensure_task_machine_project(client)
    entries = _trigger_task_entries(task_id=261)
    entries["enabled"] = False
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    assert all(log["entries"]["task_id"] != 261 for log in executions)


@pytest.mark.anyio
async def test_disabling_scheduled_task_clears_execution(
    client: AsyncClient,
):
    """Toggling enabled=False on an armed scheduled task clears its activation."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=262),
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    assert any(log["entries"]["task_id"] == 262 for log in executions)

    response = await _update_logs(
        client,
        [log_id],
        {"enabled": False},
        context=TASKS_CONTEXT,
        overwrite=True,
    )
    assert response.status_code == 200, response.json()

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    assert all(log["entries"]["task_id"] != 262 for log in executions)


def test_is_task_enabled_defaults_missing_to_true():
    """Legacy rows without an enabled column remain activatable."""

    assert task_machine_state_service._is_task_enabled({}) is True
    assert task_machine_state_service._is_task_enabled({"enabled": True}) is True
    assert task_machine_state_service._is_task_enabled({"enabled": False}) is False
    assert task_machine_state_service._is_task_enabled({"enabled": "false"}) is False
    assert (
        task_machine_state_service._is_scheduled_execution_candidate(
            {
                "status": "scheduled",
                "schedule": {"start_at": "2026-04-10T09:00:00+00:00"},
                "enabled": False,
            },
        )
        is False
    )
    assert (
        task_machine_state_service._is_trigger_execution_candidate(
            {
                "status": "triggerable",
                "trigger": {"medium": "email"},
                "enabled": False,
            },
        )
        is False
    )


@pytest.mark.anyio
async def test_task_create_projects_offline_agentic_execution(
    client: AsyncClient,
    materialization_calls,
):
    """Offline delivery should not require a symbolic function entrypoint."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_offline_task_entries(task_id=250, entrypoint=None),
    )
    assert response.status_code == 200, response.json()

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    matching = [
        log["entries"] for log in executions if log["entries"]["task_id"] == 250
    ]
    assert len(matching) == 1
    execution = matching[0]
    assert execution["delivery"] == "offline"
    assert execution["entrypoint"] is None
    assert materialization_calls == [(None, execution)]


@pytest.mark.anyio
async def test_task_delete_clears_execution(client: AsyncClient):
    """Deleting a task row should remove its activation row."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=303),
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    response = await _delete_logs(
        client,
        [(log_id, None)],
        project_name=TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
    )
    assert response.status_code == 200, response.json()

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    assert all(log["entries"]["task_id"] != 303 for log in executions)


@pytest.mark.anyio
async def test_admin_reproject_restores_missing_scheduled_execution(
    client: AsyncClient,
    materialization_calls,
):
    """Reprojection should rebuild an activation row from the current Tasks row."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=350),
    )
    assert response.status_code == 200, response.json()

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    execution_log = next(log for log in executions if log["entries"]["task_id"] == 350)
    delete_response = await _delete_logs(
        client,
        [(execution_log["id"], None)],
        project_name=TASK_MACHINE_PROJECT_NAME,
        context=TASK_EXECUTIONS_CONTEXT,
    )
    assert delete_response.status_code == 200, delete_response.json()
    materialization_calls.clear()

    reproject_response = await client.post(
        "/v0/admin/task-execution/reproject",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "task_id": 350,
        },
        headers=ADMIN_HEADERS,
    )

    assert reproject_response.status_code == 200, reproject_response.json()
    body = reproject_response.json()
    assert body["upserted"] == 1
    assert body["deleted"] == 0
    assert body["execution"]["task_id"] == 350
    assert body["execution"]["wake"] == "scheduled"
    assert materialization_calls == [(None, body["execution"])]

    second_response = await client.post(
        "/v0/admin/task-execution/reproject",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "task_id": 350,
        },
        headers=ADMIN_HEADERS,
    )
    assert second_response.status_code == 200, second_response.json()
    executions_after_second = await _get_context_logs(
        client,
        context_name=TASK_EXECUTIONS_CONTEXT,
    )
    matching = [
        log for log in executions_after_second if log["entries"]["task_id"] == 350
    ]
    assert len(matching) == 1


@pytest.mark.anyio
async def test_task_projection_chooses_latest_armed_triggerable_instance(
    client: AsyncClient,
):
    """Projection should follow the current armed row for a shared logical task."""

    await _ensure_task_machine_project(client)
    first = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_trigger_task_entries(task_id=404, instance_id=0, status="active"),
    )
    assert first.status_code == 200, first.json()

    second = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_trigger_task_entries(task_id=404, instance_id=1, status="triggerable"),
    )
    assert second.status_code == 200, second.json()
    second_log_id = second.json()["log_event_ids"][0]

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    matching = [
        log["entries"] for log in executions if log["entries"]["task_id"] == 404
    ]
    assert len(matching) == 1
    execution = matching[0]
    assert execution["assistant_id"] == "42"
    assert execution["source_task_log_id"] == second_log_id
    assert execution["wake"] == "triggered"
    assert execution["trigger_medium"] == "email"
    assert execution["trigger_from_contact_ids"] == [17]
    assert execution["interrupt"] is True
    assert execution["trigger_recurring"] is True


@pytest.mark.anyio
async def test_delete_context_blocks_internal_task_machine_context(
    client: AsyncClient,
):
    """Internal task machine contexts should be protected from direct deletion."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=505),
    )
    assert response.status_code == 200, response.json()

    response = await client.delete(
        f"/v0/project/{TASK_MACHINE_PROJECT_NAME}/contexts/{TASK_EXECUTIONS_CONTEXT}",
        headers=HEADERS,
    )
    assert response.status_code == 403
    assert "Cannot delete protected task machine contexts." in response.json()["detail"]


@pytest.mark.anyio
async def test_task_run_create_or_adopt_is_idempotent(client: AsyncClient):
    """The internal run API should reuse the same row for duplicate run_keys."""

    await _ensure_task_machine_project(client)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=101),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]
    payload = {
        "project_name": TASK_MACHINE_PROJECT_NAME,
        "run_key": "offline:42:101:rev-1",
        "assistant_id": "42",
        "task_id": 101,
        "source_task_log_id": source_task_log_id,
        "wake": "scheduled",
        "delivery": "offline",
        "revision": "rev-1",
        "scheduled_for": "2026-04-10T09:00:00+00:00",
        "source_medium": "email",
        "source_ref": "message-101",
        "source_contact_id": "17",
        "source_contact_display_name": "Alice Owner",
        "task_name": "Morning briefing",
        "task_description": "Prepare the team's daily summary.",
        "state": "scheduled",
    }

    first = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 200, first.json()
    first_body = first.json()
    assert first_body["created"] is True
    first_run = first_body["run"]
    assert first_run["run_key"] == payload["run_key"]
    assert first_run["run_id"]
    assert first_run["delivery"] == "offline"
    assert first_run["source_medium"] == "email"
    assert first_run["source_ref"] == "message-101"
    assert first_run["source_contact_id"] == "17"
    assert first_run["source_contact_display_name"] == "Alice Owner"
    assert first_run["task_name"] == "Morning briefing"
    assert first_run["task_description"] == "Prepare the team's daily summary."

    second = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200, second.json()
    second_body = second.json()
    assert second_body["created"] is False
    assert second_body["run"]["run_id"] == first_run["run_id"]


@pytest.mark.anyio
async def test_task_run_get_returns_precreated_run_or_none(
    client: AsyncClient,
    dbsession,
):
    """Task-run get returns an existing row by run_key without creating or adopting."""

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=f"Assistants/{assistant.agent_id}/Tasks",
        entries=_assistant_scoped_scheduled_entries(
            user_id=PRIMARY_USER_ID,
            assistant_id=assistant.agent_id,
            task_id=101,
        ),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]
    run_key = "offline:provider_event:42:101:binding-a:rev123:abcdef0123456789"
    create_response = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": run_key,
            "assistant_id": str(assistant.agent_id),
            "task_id": 101,
            "source_task_log_id": source_task_log_id,
            "wake": "provider_event",
            "delivery": "offline",
            "revision": "rev-123",
            "state": "scheduled",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()
    created_run = create_response.json()["run"]

    get_response = await client.post(
        "/v0/admin/task-execution/get",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "run_key": run_key,
            "source_task_log_id": source_task_log_id,
        },
        headers=ADMIN_HEADERS,
    )
    assert get_response.status_code == 200, get_response.json()
    fetched_run = get_response.json()["run"]
    assert fetched_run["run_id"] == created_run["run_id"]
    assert fetched_run["run_key"] == run_key

    missing_response = await client.post(
        "/v0/admin/task-execution/get",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "run_key": "offline:provider_event:missing",
        },
        headers=ADMIN_HEADERS,
    )
    assert missing_response.status_code == 200, missing_response.json()
    assert missing_response.json()["run"] is None


@pytest.mark.anyio
async def test_team_task_run_lifecycle_stays_on_team_surface(
    client: AsyncClient,
    dbsession,
):
    """Team-task executions are created AND updated under ``Teams/{id}/Tasks/Executions``.

    Creation resolves the Executions context from the task's own surface via
    ``source_task_log_id``; updates must resolve the same row whether or not
    they carry ``source_task_log_id`` (older runtimes omit it — the
    key-based team-surface fallback covers them).
    """

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    team = _make_team_member(dbsession, assistant=assistant)
    team_tasks_context = f"Teams/{team.id}/Tasks"
    entries = _assistant_scoped_scheduled_entries(
        user_id=PRIMARY_USER_ID,
        assistant_id=assistant.agent_id,
        task_id=321,
    )
    entries["assistant_id"] = str(assistant.agent_id)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=team_tasks_context,
        entries=entries,
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]

    run_key = f"offline:scheduled:{assistant.agent_id}:team:{team.id}:321:rev-1"
    create_response = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": run_key,
            "assistant_id": str(assistant.agent_id),
            "task_id": 321,
            "source_task_log_id": source_task_log_id,
            "wake": "scheduled",
            "delivery": "offline",
            "destination": f"team:{team.id}",
            "state": "running",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()

    # The execution row lives on the team surface, where Console's team Activity
    # view reads it — not in the executor's personal root.
    team_runs = await _get_context_logs(
        client,
        context_name=f"{team_tasks_context}/Executions",
    )
    assert [log["entries"]["run_key"] for log in team_runs] == [run_key]

    # Update WITHOUT source_task_log_id (older runtime): the key-based
    # team-surface fallback must find the row.
    fallback_update = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "run_key": run_key,
            "updates": {"state": "running", "progress_summary": "halfway"},
        },
        headers=ADMIN_HEADERS,
    )
    assert fallback_update.status_code == 200, fallback_update.json()
    assert fallback_update.json()["run"]["progress_summary"] == "halfway"

    # Update WITH source_task_log_id (current runtimes): direct resolution.
    direct_update = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "run_key": run_key,
            "source_task_log_id": source_task_log_id,
            "updates": {
                "state": "succeeded",
                "completed_at": "2026-04-10T09:05:00+00:00",
            },
        },
        headers=ADMIN_HEADERS,
    )
    assert direct_update.status_code == 200, direct_update.json()
    assert direct_update.json()["run"]["state"] == "succeeded"

    # Both updates mutated the single team-surface row in place.
    team_runs_after = await _get_context_logs(
        client,
        context_name=f"{team_tasks_context}/Executions",
    )
    assert len(team_runs_after) == 1
    final_run = team_runs_after[0]["entries"]
    assert final_run["state"] == "succeeded"
    assert final_run["progress_summary"] == "halfway"


@pytest.mark.anyio
async def test_task_run_update_mutates_existing_row(client: AsyncClient):
    """The internal run API should merge partial updates into an existing row."""

    await _ensure_task_machine_project(client)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=202),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]
    run_key = "offline:42:202:rev-2"
    create_response = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": run_key,
            "assistant_id": "42",
            "task_id": 202,
            "source_task_log_id": source_task_log_id,
            "wake": "triggered",
            "delivery": "offline",
            "state": "running",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()

    update_response = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "run_key": run_key,
            "updates": {
                "state": "completed",
                "completed_at": "2026-04-10T09:05:00+00:00",
                "result_summary": "ok",
            },
        },
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == 200, update_response.json()
    updated_run = update_response.json()["run"]
    assert updated_run["run_key"] == run_key
    assert updated_run["state"] == "completed"
    assert updated_run["completed_at"] == "2026-04-10T09:05:00+00:00"
    assert updated_run["result_summary"] == "ok"


@pytest.mark.anyio
async def test_task_run_latest_returns_most_recent_task_run(client: AsyncClient):
    """The internal run lookup should return the latest run for a logical task."""

    await _ensure_task_machine_project(client)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=303),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]

    for run_key in ("live:42:303:first", "live:42:303:second"):
        create_response = await client.post(
            "/v0/admin/task-execution/create-or-adopt",
            json={
                "project_name": TASK_MACHINE_PROJECT_NAME,
                "run_key": run_key,
                "assistant_id": "42",
                "task_id": 303,
                "source_task_log_id": source_task_log_id,
                "wake": "scheduled",
                "delivery": "live",
                "state": "scheduled",
            },
            headers=ADMIN_HEADERS,
        )
        assert create_response.status_code == 200, create_response.json()

    update_response = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "run_key": "live:42:303:second",
            "updates": {"state": "completed"},
        },
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == 200, update_response.json()

    latest_response = await client.post(
        "/v0/admin/task-execution/latest",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "task_id": 303,
            "source_task_log_id": source_task_log_id,
        },
        headers=ADMIN_HEADERS,
    )
    assert latest_response.status_code == 200, latest_response.json()
    assert latest_response.json()["run"]["run_key"] == "live:42:303:second"
    assert latest_response.json()["run"]["state"] == "completed"


@pytest.mark.anyio
async def test_task_run_latest_filters_by_source_task_log_id(client: AsyncClient):
    """Source-scoped latest lookup should not cross physical task instances."""

    await _ensure_task_machine_project(client)
    first_source = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(
            task_id=313,
            instance_id=0,
            start_at="2026-04-10T09:00:00+00:00",
        ),
    )
    assert first_source.status_code == 200, first_source.json()
    first_source_log_id = first_source.json()["log_event_ids"][0]

    second_source = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(
            task_id=313,
            instance_id=1,
            start_at="2026-04-10T09:30:00+00:00",
        ),
    )
    assert second_source.status_code == 200, second_source.json()
    second_source_log_id = second_source.json()["log_event_ids"][0]

    for run_key, source_task_log_id in (
        ("live:42:313:first", first_source_log_id),
        ("live:42:313:second", second_source_log_id),
    ):
        create_response = await client.post(
            "/v0/admin/task-execution/create-or-adopt",
            json={
                "project_name": TASK_MACHINE_PROJECT_NAME,
                "run_key": run_key,
                "assistant_id": "42",
                "task_id": 313,
                "source_task_log_id": source_task_log_id,
                "wake": "scheduled",
                "delivery": "live",
                "state": "scheduled",
            },
            headers=ADMIN_HEADERS,
        )
        assert create_response.status_code == 200, create_response.json()

    update_response = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "run_key": "live:42:313:second",
            "updates": {"state": "completed"},
        },
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == 200, update_response.json()

    scoped_response = await client.post(
        "/v0/admin/task-execution/latest",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "task_id": 313,
            "source_task_log_id": first_source_log_id,
        },
        headers=ADMIN_HEADERS,
    )
    assert scoped_response.status_code == 200, scoped_response.json()
    assert scoped_response.json()["run"]["run_key"] == "live:42:313:first"

    unscoped_response = await client.post(
        "/v0/admin/task-execution/latest",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "task_id": 313,
        },
        headers=ADMIN_HEADERS,
    )
    assert unscoped_response.status_code == 200, unscoped_response.json()
    assert unscoped_response.json()["run"]["run_key"] == "live:42:313:second"


@pytest.mark.anyio
async def test_task_outbound_operation_create_or_adopt_is_idempotent(
    client: AsyncClient,
):
    """The internal outbound API should reuse the same row for duplicate keys."""

    await _ensure_task_machine_project(client)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=303),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]
    payload = {
        "project_name": TASK_MACHINE_PROJECT_NAME,
        "operation_key": "offline:42:303:run-1:1",
        "assistant_id": "42",
        "task_run_key": "offline:42:303:run-1",
        "task_id": 303,
        "source_task_log_id": source_task_log_id,
        "operation_index": 1,
        "method_name": "send_email",
        "medium": "email",
        "target_kind": "contact",
        "contact_id": 17,
        "target_metadata": {
            "email": "alice@example.com",
            "display_name": "Alice Owner",
        },
        "status": "pending",
    }

    first = await client.post(
        "/v0/admin/task-outbound-operation/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 200, first.json()
    first_body = first.json()
    assert first_body["created"] is True
    first_operation = first_body["operation"]
    assert first_operation["operation_key"] == payload["operation_key"]
    assert first_operation["operation_id"]
    assert first_operation["task_run_key"] == payload["task_run_key"]
    assert first_operation["medium"] == "email"
    assert first_operation["target_metadata"]["email"] == "alice@example.com"

    second = await client.post(
        "/v0/admin/task-outbound-operation/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200, second.json()
    second_body = second.json()
    assert second_body["created"] is False
    assert second_body["operation"]["operation_id"] == first_operation["operation_id"]

    rows = await _get_context_logs(
        client,
        context_name=TASK_OUTBOUND_OPERATIONS_CONTEXT,
    )
    assert len(rows) == 1
    assert rows[0]["entries"]["operation_key"] == payload["operation_key"]


def test_task_outbound_operation_create_or_adopt_reports_adoption_after_upsert_race(
    monkeypatch,
):
    """A uniqueness race should surface as adoption, not fresh creation."""

    fake_session = SimpleNamespace(flush=lambda: None)
    fake_context_ids = SimpleNamespace(outbound_operations_context_id=77)
    adopted_row = SimpleNamespace(
        id=91,
        data={
            "operation_id": 91,
            "operation_key": "offline:42:303:run-1:1",
        },
    )

    monkeypatch.setattr(
        task_machine_state_service,
        "resolve_tasks_context_name",
        lambda **kwargs: TASKS_CONTEXT,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "ensure_task_machine_contexts",
        lambda **kwargs: fake_context_ids,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "_get_machine_row_by_unique_field",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "_migrate_legacy_machine_row_if_present",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "_upsert_machine_row",
        lambda **kwargs: task_machine_state_service._MachineRowUpsertResult(
            row=adopted_row,
            created=False,
        ),
    )

    (
        operation,
        created,
    ) = task_machine_state_service.create_task_outbound_operation_if_absent(
        session=fake_session,
        project_id=1,
        payload={
            "operation_key": "offline:42:303:run-1:1",
            "assistant_id": "42",
            "task_run_key": "offline:42:303:run-1",
            "operation_index": 1,
            "method_name": "send_email",
            "medium": "email",
            "target_kind": "contact",
        },
    )

    assert operation is adopted_row
    assert created is False


def test_get_open_task_execution_is_read_only(monkeypatch):
    """Open-execution lookup must not create contexts or upsert field types."""

    fake_session = SimpleNamespace()
    ensure_calls: list[dict] = []
    execution_row = SimpleNamespace(
        id=11,
        data={
            "run_key": "offline:scheduled:42:7:rev",
            "revision": "rev-1",
            "state": "scheduled",
        },
    )

    monkeypatch.setattr(
        task_machine_state_service,
        "resolve_tasks_context_name",
        lambda **kwargs: TASKS_CONTEXT,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "ensure_task_machine_contexts",
        lambda **kwargs: ensure_calls.append(kwargs)
        or SimpleNamespace(
            executions_context_id=99,
        ),
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "lookup_task_machine_executions_context_id",
        lambda **kwargs: 55,
    )

    class _Query:
        def join(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return [execution_row]

    fake_session.query = lambda *args, **kwargs: _Query()

    execution = task_machine_state_service.get_open_task_execution(
        session=fake_session,
        project_id=1,
        assistant_id="42",
        task_id=7,
    )

    assert execution is execution_row
    assert ensure_calls == []


def test_get_open_task_execution_returns_none_without_creating_contexts(monkeypatch):
    """Missing execution contexts should yield None without schema writes."""

    fake_session = SimpleNamespace()
    ensure_calls: list[dict] = []

    monkeypatch.setattr(
        task_machine_state_service,
        "resolve_tasks_context_name",
        lambda **kwargs: TASKS_CONTEXT,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "ensure_task_machine_contexts",
        lambda **kwargs: ensure_calls.append(kwargs)
        or SimpleNamespace(
            executions_context_id=99,
        ),
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "lookup_task_machine_executions_context_id",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        task_machine_state_service,
        "_get_context_id",
        lambda **kwargs: None,
    )

    execution = task_machine_state_service.get_open_task_execution(
        session=fake_session,
        project_id=1,
        assistant_id="42",
        task_id=7,
    )

    assert execution is None
    assert ensure_calls == []


@pytest.mark.anyio
async def test_task_outbound_operation_update_mutates_existing_row(
    client: AsyncClient,
):
    """The internal outbound API should merge partial updates into one row."""

    await _ensure_task_machine_project(client)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=404),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]
    operation_key = "offline:42:404:run-2:1"
    create_response = await client.post(
        "/v0/admin/task-outbound-operation/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "operation_key": operation_key,
            "assistant_id": "42",
            "task_run_key": "offline:42:404:run-2",
            "task_id": 404,
            "source_task_log_id": source_task_log_id,
            "operation_index": 1,
            "method_name": "send_sms",
            "medium": "sms",
            "target_kind": "contact",
            "contact_id": 55,
            "target_metadata": {"phone_number": "+15555550123"},
            "status": "pending",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()

    update_response = await client.post(
        "/v0/admin/task-outbound-operation/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "operation_key": operation_key,
            "updates": {
                "status": "completed",
                "provider_message_id": "sm-123",
                "completed_at": "2026-04-10T09:06:00+00:00",
                "history_exchange_id": 7,
                "history_message_id": 9,
            },
        },
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == 200, update_response.json()
    updated_operation = update_response.json()["operation"]
    assert updated_operation["operation_key"] == operation_key
    assert updated_operation["status"] == "completed"
    assert updated_operation["provider_message_id"] == "sm-123"
    assert updated_operation["completed_at"] == "2026-04-10T09:06:00+00:00"
    assert updated_operation["history_exchange_id"] == 7
    assert updated_operation["history_message_id"] == 9


@pytest.mark.anyio
async def test_task_outbound_operation_update_rejects_immutable_field_changes(
    client: AsyncClient,
):
    """Immutable outbound identity fields should reject patch-time changes."""

    await _ensure_task_machine_project(client)
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=405),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]
    operation_key = "offline:42:405:run-3:1"
    create_response = await client.post(
        "/v0/admin/task-outbound-operation/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "operation_key": operation_key,
            "assistant_id": "42",
            "task_run_key": "offline:42:405:run-3",
            "task_id": 405,
            "source_task_log_id": source_task_log_id,
            "operation_index": 1,
            "method_name": "send_sms",
            "medium": "sms",
            "target_kind": "contact",
            "contact_id": 55,
            "target_metadata": {"phone_number": "+15555550123"},
            "status": "pending",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()

    update_response = await client.post(
        "/v0/admin/task-outbound-operation/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": "42",
            "operation_key": operation_key,
            "updates": {
                "operation_key": "offline:42:405:run-3:mutated",
            },
        },
        headers=ADMIN_HEADERS,
    )

    assert update_response.status_code == 400, update_response.json()
    assert "immutable" in update_response.json()["detail"]
    rows = await _get_context_logs(
        client,
        context_name=TASK_OUTBOUND_OPERATIONS_CONTEXT,
    )
    assert len(rows) == 1
    assert rows[0]["entries"]["operation_key"] == operation_key


@pytest.mark.anyio
async def test_task_execution_lookup_resolves_assistant_scoped_project(
    client: AsyncClient,
    dbsession,
):
    """Admin activation lookup should use the assistant owner's Assistants project."""

    await _ensure_task_machine_project(client)
    secondary_project = await _create_project(client, TASK_MACHINE_PROJECT_NAME, user=2)
    assert secondary_project.status_code in (200, 400), secondary_project.json()

    assistant = _make_assistant(dbsession, user_id=SECONDARY_USER_ID)
    task_id = 909
    tasks_context = _assistant_tasks_context(
        user_id=SECONDARY_USER_ID,
        assistant_id=assistant.agent_id,
    )
    create_response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        user=2,
        context=tasks_context,
        entries=_assistant_scoped_scheduled_entries(
            user_id=SECONDARY_USER_ID,
            assistant_id=assistant.agent_id,
            task_id=task_id,
        ),
    )
    assert create_response.status_code == 200, create_response.json()

    lookup_response = await client.post(
        "/v0/admin/task-execution/current",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
        },
        headers=ADMIN_HEADERS,
    )
    assert lookup_response.status_code == 200, lookup_response.json()
    execution = lookup_response.json()["execution"]
    assert execution is not None
    assert execution["assistant_id"] == str(assistant.agent_id)
    assert execution["task_id"] == task_id


@pytest.mark.anyio
async def test_task_run_admin_mutations_resolve_assistant_scoped_project(
    client: AsyncClient,
    dbsession,
):
    """Admin run mutations should land in the assistant owner's Assistants project."""

    await _ensure_task_machine_project(client)
    secondary_project = await _create_project(client, TASK_MACHINE_PROJECT_NAME, user=2)
    assert secondary_project.status_code in (200, 400), secondary_project.json()

    assistant = _make_assistant(dbsession, user_id=SECONDARY_USER_ID)
    task_id = 910
    tasks_context = _assistant_tasks_context(
        user_id=SECONDARY_USER_ID,
        assistant_id=assistant.agent_id,
    )
    source_task = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        user=2,
        context=tasks_context,
        entries=_assistant_scoped_scheduled_entries(
            user_id=SECONDARY_USER_ID,
            assistant_id=assistant.agent_id,
            task_id=task_id,
        ),
    )
    assert source_task.status_code == 200, source_task.json()
    source_task_log_id = source_task.json()["log_event_ids"][0]
    run_key = f"offline:{assistant.agent_id}:{task_id}:rev-2"

    create_response = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": run_key,
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "source_task_log_id": source_task_log_id,
            "wake": "scheduled",
            "delivery": "offline",
            "state": "running",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()
    assert create_response.json()["created"] is True

    update_response = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "run_key": run_key,
            "updates": {
                "state": "completed",
                "completed_at": "2026-04-10T09:10:00+00:00",
            },
        },
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == 200, update_response.json()
    assert update_response.json()["run"]["state"] == "completed"

    run_logs = await _get_context_logs(
        client,
        context_name=task_machine_state_service.build_task_executions_context_name(
            tasks_context,
        ),
        user=2,
    )
    matching = [log for log in run_logs if log["entries"].get("run_key") == run_key]
    assert len(matching) == 1
    assert matching[0]["entries"]["state"] == "completed"


@pytest.mark.anyio
async def test_series_advances_one_occurrence_at_a_time(client: AsyncClient) -> None:
    """Walk two occurrences the way dispatch does, and count the rows.

    Every defect that silently stopped a ten-minute recurring tick lived here,
    and none of them raised — the series just stopped advancing:

    * A projected occurrence born ``running`` looked like a live concurrent peer
      to its predecessor the moment that predecessor started, so an overlap
      guard skipped both, forever.
    * A dispatcher that could not rebuild the projected ``run_key`` created a
      *second* row for the same occurrence instead of adopting the first, which
      reads as concurrency for the same reason.

    So this asserts occurrence counts and states rather than absence of errors:
    one pending row per slot, adopted (not duplicated) when it starts, and a
    successor that is pending and later while its predecessor still runs.
    """

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=707),
    )
    assert response.status_code == 200, response.json()

    def _rows() -> list[dict]:
        return [
            log["entries"] for log in executions if log["entries"].get("task_id") == 707
        ]

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    projected = _rows()
    assert len(projected) == 1, f"expected one projected occurrence: {projected}"
    head = projected[0]
    assert head["state"] == "scheduled", head
    assert not head.get("started_at"), "a projected occurrence has not started"
    assert head.get("run_key"), head

    # Dispatch adopts the projected row by run_key rather than creating a twin.
    adopt = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": head["run_key"],
            "assistant_id": str(head["assistant_id"]),
            "task_id": 707,
            "source_task_log_id": head.get("source_task_log_id"),
            "wake": head["wake"],
            "delivery": head["delivery"],
            "scheduled_for": head["scheduled_for"],
            "state": "running",
            "started_at": "2026-04-10T09:00:01+00:00",
        },
        headers=ADMIN_HEADERS,
    )
    assert adopt.status_code == 200, adopt.json()
    assert adopt.json()["created"] is False, (
        "dispatch created a second execution for one occurrence instead of "
        "adopting the projected row"
    )

    # Adoption returns the row as-is; the dispatcher marks it running itself,
    # the way Communication does once the job is launched.
    started = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(head["assistant_id"]),
            "run_key": head["run_key"],
            "source_task_log_id": head.get("source_task_log_id"),
            "updates": {
                "state": "running",
                "started_at": "2026-04-10T09:00:01+00:00",
            },
        },
        headers=ADMIN_HEADERS,
    )
    assert started.status_code == 200, started.json()

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    running = _rows()
    assert len(running) == 1, f"one occurrence must own one row: {running}"
    assert running[0]["state"] == "running"

    # The successor is projected while that run is still in flight.
    successor_slot = "2026-04-10T09:10:00+00:00"
    created = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": f"{head['run_key']}-successor",
            "assistant_id": str(head["assistant_id"]),
            "task_id": 707,
            "source_task_log_id": head.get("source_task_log_id"),
            "wake": head["wake"],
            "delivery": head["delivery"],
            "scheduled_for": successor_slot,
            "state": "scheduled",
        },
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.json()
    assert created.json()["created"] is True

    executions = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    walked = _rows()
    assert len(walked) == 2, f"the series did not advance exactly one slot: {walked}"
    by_state = {row["state"]: row for row in walked}
    assert set(by_state) == {"running", "scheduled"}, walked
    successor = by_state["scheduled"]
    # Orchestra normalizes the stored instant; compare as instants, not strings.
    assert _parse_iso(successor["scheduled_for"]) == _parse_iso(successor_slot)
    assert not successor.get("started_at"), (
        "the successor was born running; it and its predecessor read as "
        "concurrent peers and skip each other"
    )
    assert (
        by_state["running"]["run_key"] == head["run_key"]
    ), "projecting the successor replaced or terminalized the in-flight run"


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant, accepting either a Z or an explicit offset."""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
