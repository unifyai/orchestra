"""Integration tests for Unity task machine-state projection."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestra.services import task_machine_state_service
from orchestra.tests.test_log import (
    HEADERS,
    _create_log,
    _create_project,
    _delete_logs,
    _update_logs,
)
from orchestra.tests.utils import ADMIN_HEADERS

TASKS_CONTEXT = "1/42/Tasks"


async def _ensure_unity_project(client: AsyncClient) -> None:
    """Create the protected Unity project when it does not already exist."""

    response = await _create_project(client, "Unity")
    assert response.status_code in (200, 400), response.json()


async def _get_context_logs(
    client: AsyncClient,
    *,
    context_name: str,
) -> list[dict]:
    """Fetch logs from a Unity context and return the payload list."""

    response = await client.get(
        "/v0/logs",
        params={"project_name": "Unity", "context": context_name},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    return response.json()["logs"]


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
    """Return a minimal scheduled Unity task row."""

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
    """Return a minimal triggerable Unity task row."""

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


@pytest.mark.anyio
async def test_unity_task_create_projects_scheduled_activation(
    client: AsyncClient,
    materialization_calls,
):
    """Creating a scheduled task should materialize one activation row."""

    await _ensure_unity_project(client)
    response = await _create_log(
        client,
        "Unity",
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=101),
    )
    assert response.status_code == 200, response.json()
    created_task_log_id = response.json()["log_event_ids"][0]

    activations = await _get_context_logs(client, context_name="Tasks/Activations")
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
async def test_unity_task_update_reconciles_new_schedule_head(
    client: AsyncClient,
    materialization_calls,
):
    """Schedule edits should carry both the old and new queue-head due times."""

    await _ensure_unity_project(client)
    response = await _create_log(
        client,
        "Unity",
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
async def test_unity_task_update_clears_activation_when_row_stops_being_armed(
    client: AsyncClient,
):
    """Updating a task into a non-activatable status should clear the activation."""

    await _ensure_unity_project(client)
    response = await _create_log(
        client,
        "Unity",
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

    activations = await _get_context_logs(client, context_name="Tasks/Activations")
    assert all(log["entries"]["task_id"] != 202 for log in activations)


@pytest.mark.anyio
async def test_unity_task_create_rejects_offline_row_without_integer_entrypoint(
    client: AsyncClient,
):
    """Offline task rows must supply an integer entrypoint before projection."""

    await _ensure_unity_project(client)
    response = await _create_log(
        client,
        "Unity",
        context=TASKS_CONTEXT,
        entries=_offline_task_entries(task_id=250, entrypoint=None),
    )
    assert response.status_code == 400
    assert "Offline tasks require an integer entrypoint" in response.json()["detail"]


@pytest.mark.anyio
async def test_unity_task_delete_clears_activation(client: AsyncClient):
    """Deleting a task row should remove its activation row."""

    await _ensure_unity_project(client)
    response = await _create_log(
        client,
        "Unity",
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=303),
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()["log_event_ids"][0]

    response = await _delete_logs(
        client,
        [(log_id, None)],
        project_name="Unity",
        context=TASKS_CONTEXT,
    )
    assert response.status_code == 200, response.json()

    activations = await _get_context_logs(client, context_name="Tasks/Activations")
    assert all(log["entries"]["task_id"] != 303 for log in activations)


@pytest.mark.anyio
async def test_unity_task_projection_chooses_latest_armed_triggerable_instance(
    client: AsyncClient,
):
    """Projection should follow the current armed row for a shared logical task."""

    await _ensure_unity_project(client)
    first = await _create_log(
        client,
        "Unity",
        context=TASKS_CONTEXT,
        entries=_trigger_task_entries(task_id=404, instance_id=0, status="active"),
    )
    assert first.status_code == 200, first.json()

    second = await _create_log(
        client,
        "Unity",
        context=TASKS_CONTEXT,
        entries=_trigger_task_entries(task_id=404, instance_id=1, status="triggerable"),
    )
    assert second.status_code == 200, second.json()
    second_log_id = second.json()["log_event_ids"][0]

    activations = await _get_context_logs(client, context_name="Tasks/Activations")
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

    await _ensure_unity_project(client)
    response = await _create_log(
        client,
        "Unity",
        context=TASKS_CONTEXT,
        entries=_scheduled_task_entries(task_id=505),
    )
    assert response.status_code == 200, response.json()

    response = await client.delete(
        "/v0/project/Unity/contexts/Tasks/Activations",
        headers=HEADERS,
    )
    assert response.status_code == 403
    assert "Cannot delete built-in Tasks context" in response.json()["detail"]


@pytest.mark.anyio
async def test_task_run_create_or_adopt_is_idempotent(client: AsyncClient):
    """The internal run API should reuse the same row for duplicate run_keys."""

    await _ensure_unity_project(client)
    payload = {
        "project_name": "Unity",
        "run_key": "offline:42:101:rev-1",
        "assistant_id": "42",
        "task_id": 101,
        "source_task_log_id": 555,
        "source_type": "scheduled",
        "execution_mode": "offline",
        "activation_revision": "rev-1",
        "scheduled_for": "2026-04-10T09:00:00+00:00",
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

    await _ensure_unity_project(client)
    run_key = "offline:42:202:rev-2"
    create_response = await client.post(
        "/v0/admin/task-run/create-or-adopt",
        json={
            "project_name": "Unity",
            "run_key": run_key,
            "assistant_id": "42",
            "task_id": 202,
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
            "project_name": "Unity",
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
