"""Integration tests for Unity task machine-state projection."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import Assistant
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
TASK_ACTIVATIONS_CONTEXT = (
    task_machine_state_service.build_task_activation_context_name(
        TASKS_CONTEXT,
    )
)
TASK_RUNS_CONTEXT = task_machine_state_service.build_task_runs_context_name(
    TASKS_CONTEXT,
)
SECONDARY_USER_ID = "seconday_user"


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


@pytest.fixture(autouse=True)
def materialization_calls(monkeypatch):
    """Capture scheduled activation sync requests without hitting Communication."""

    calls: list[tuple[dict | None, dict | None]] = []

    def _capture(*, previous_activation, current_activation):
        calls.append((previous_activation, current_activation))

    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_activation_materialization",
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
            "prev_task": None,
            "next_task": None,
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


def test_scheduled_activation_upsert_body_includes_wake_context():
    """Scheduled activation sync should carry compact human-facing wake context."""

    body = task_machine_state_service._scheduled_activation_upsert_body(
        {
            "assistant_id": "42",
            "task_id": 101,
            "source_task_log_id": 555,
            "activation_kind": "scheduled",
            "execution_mode": "live",
            "activation_revision": "rev-1",
            "next_due_at": "2026-04-10T09:00:00+00:00",
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


@pytest.mark.anyio
async def test_task_create_projects_scheduled_activation(
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

    activations = await _get_context_logs(client, context_name=TASK_ACTIVATIONS_CONTEXT)
    assert len(activations) == 1
    activation = activations[0]["entries"]
    assert activation["assistant_id"] == "42"
    assert activation["activation_key"] == "42:101"
    assert activation["task_id"] == 101
    assert activation["source_task_log_id"] == created_task_log_id
    assert activation["activation_kind"] == "scheduled"
    assert activation["execution_mode"] == "live"
    assert activation["next_due_at"] == "2026-04-10T09:00:00+00:00"
    assert activation["repeat"] == [{"unit": "day", "count": 1}]
    assert activation["activation_revision"]
    assert materialization_calls == [(None, activation)]


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
    previous_activation, current_activation = materialization_calls[0]
    assert previous_activation["next_due_at"] == "2026-04-10T09:00:00+00:00"
    assert current_activation["next_due_at"] == "2026-04-10T11:30:00+00:00"
    assert current_activation["activation_key"] == "42:151"


@pytest.mark.anyio
async def test_task_update_clears_activation_when_row_stops_being_armed(
    client: AsyncClient,
):
    """Updating a task into a non-activatable status should clear the activation."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=202),
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    response = await _update_logs(
        client,
        [log_id],
        {"status": "active"},
        context=TASKS_CONTEXT,
        overwrite=True,
    )
    assert response.status_code == 200, response.json()

    activations = await _get_context_logs(client, context_name=TASK_ACTIVATIONS_CONTEXT)
    assert all(log["entries"]["task_id"] != 202 for log in activations)


@pytest.mark.anyio
async def test_task_create_rejects_offline_row_without_integer_entrypoint(
    client: AsyncClient,
):
    """Offline task rows must supply an integer entrypoint before projection."""

    await _ensure_task_machine_project(client)
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=_offline_task_entries(task_id=250, entrypoint=None),
    )
    assert response.status_code == 400
    assert "Offline tasks require an integer entrypoint" in response.json()["detail"]


@pytest.mark.anyio
async def test_task_delete_clears_activation(client: AsyncClient):
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

    activations = await _get_context_logs(client, context_name=TASK_ACTIVATIONS_CONTEXT)
    assert all(log["entries"]["task_id"] != 303 for log in activations)


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

    activations = await _get_context_logs(client, context_name=TASK_ACTIVATIONS_CONTEXT)
    matching = [
        log["entries"] for log in activations if log["entries"]["task_id"] == 404
    ]
    assert len(matching) == 1
    activation = matching[0]
    assert activation["assistant_id"] == "42"
    assert activation["activation_key"] == "42:404"
    assert activation["source_task_log_id"] == second_log_id
    assert activation["instance_id"] == 1
    assert activation["activation_kind"] == "triggered"
    assert activation["trigger_medium"] == "email"
    assert activation["trigger_from_contact_ids"] == [17]
    assert activation["interrupt"] is True
    assert activation["trigger_recurring"] is True


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
        f"/v0/project/{TASK_MACHINE_PROJECT_NAME}/contexts/{TASK_ACTIVATIONS_CONTEXT}",
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
        "source_type": "scheduled",
        "execution_mode": "offline",
        "activation_revision": "rev-1",
        "scheduled_for": "2026-04-10T09:00:00+00:00",
        "source_medium": "email",
        "source_ref": "message-101",
        "source_contact_id": "17",
        "source_contact_display_name": "Alice Owner",
        "task_name": "Morning briefing",
        "task_description": "Prepare the team's daily summary.",
        "state": "pending",
    }

    first = await client.post(
        "/v0/admin/task-run/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 200, first.json()
    first_body = first.json()
    assert first_body["created"] is True
    first_run = first_body["run"]
    assert first_run["run_key"] == payload["run_key"]
    assert first_run["run_id"]
    assert first_run["execution_mode"] == "offline"
    assert first_run["source_medium"] == "email"
    assert first_run["source_ref"] == "message-101"
    assert first_run["source_contact_id"] == "17"
    assert first_run["source_contact_display_name"] == "Alice Owner"
    assert first_run["task_name"] == "Morning briefing"
    assert first_run["task_description"] == "Prepare the team's daily summary."

    second = await client.post(
        "/v0/admin/task-run/create-or-adopt",
        json=payload,
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200, second.json()
    second_body = second.json()
    assert second_body["created"] is False
    assert second_body["run"]["run_id"] == first_run["run_id"]


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
        "/v0/admin/task-run/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": run_key,
            "assistant_id": "42",
            "task_id": 202,
            "source_task_log_id": source_task_log_id,
            "source_type": "triggered",
            "execution_mode": "offline",
            "state": "running",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()

    update_response = await client.post(
        "/v0/admin/task-run/update",
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
async def test_task_activation_lookup_resolves_assistant_scoped_project(
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
        "/v0/admin/task-activation/current",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
        },
        headers=ADMIN_HEADERS,
    )
    assert lookup_response.status_code == 200, lookup_response.json()
    activation = lookup_response.json()["activation"]
    assert activation is not None
    assert activation["assistant_id"] == str(assistant.agent_id)
    assert activation["task_id"] == task_id


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
        "/v0/admin/task-run/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": run_key,
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "source_task_log_id": source_task_log_id,
            "source_type": "scheduled",
            "execution_mode": "offline",
            "state": "running",
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200, create_response.json()
    assert create_response.json()["created"] is True

    update_response = await client.post(
        "/v0/admin/task-run/update",
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
        context_name=task_machine_state_service.build_task_runs_context_name(
            tasks_context,
        ),
        user=2,
    )
    assert len(run_logs) == 1
    assert run_logs[0]["entries"]["run_key"] == run_key
    assert run_logs[0]["entries"]["state"] == "completed"
