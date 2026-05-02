"""Integration tests for Unity task machine-state projection."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    Context,
    Space,
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
TASK_ACTIVATIONS_CONTEXT = (
    task_machine_state_service.build_task_activation_context_name(
        TASKS_CONTEXT,
    )
)
TASK_RUNS_CONTEXT = task_machine_state_service.build_task_runs_context_name(
    TASKS_CONTEXT,
)
TASK_OUTBOUND_OPERATIONS_CONTEXT = (
    task_machine_state_service.build_task_outbound_operations_context_name(
        TASKS_CONTEXT,
    )
)
PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID") or "1")
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


def _make_space_member(
    dbsession,
    *,
    assistant: Assistant,
    owner_user_id: str = PRIMARY_USER_ID,
) -> Space:
    """Create a shared space and attach the assistant as a live member."""

    space = Space(
        name="Project Room",
        description="Project room workspace for task routing tests.",
        owner_user_id=owner_user_id,
        status="active",
    )
    dbsession.add(space)
    dbsession.flush()
    dbsession.add(
        AssistantSpaceMembership(
            assistant_id=assistant.agent_id,
            space_id=space.space_id,
            added_by=owner_user_id,
        ),
    )
    dbsession.flush()
    return space


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
async def test_space_task_projects_activation_into_executor_context(
    client: AsyncClient,
    dbsession,
    materialization_calls,
):
    """Shared task definitions should create executor-owned activation rows."""

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    space = _make_space_member(dbsession, assistant=assistant)
    space_tasks_context = f"Spaces/{space.space_id}/Tasks"
    executor_activation_context = (
        task_machine_state_service.build_task_activation_context_name(
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
        context=space_tasks_context,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    activations = await _get_context_logs(
        client,
        context_name=executor_activation_context,
    )
    matching = [
        log["entries"] for log in activations if log["entries"]["task_id"] == 111
    ]
    assert len(matching) == 1
    activation = matching[0]
    assert activation["assistant_id"] == str(assistant.agent_id)
    assert activation["destination"] == f"space:{space.space_id}"
    assert (
        activation["activation_key"]
        == f"{assistant.agent_id}:space:{space.space_id}:111"
    )
    assert materialization_calls == [(None, activation)]

    shared_activation_context = f"Spaces/{space.space_id}/Tasks/Activations"
    assert (
        dbsession.query(Context)
        .filter(Context.name == shared_activation_context)
        .one_or_none()
        is None
    )


@pytest.mark.anyio
async def test_space_task_membership_mismatch_does_not_project_activation(
    client: AsyncClient,
    dbsession,
    materialization_calls,
):
    """Shared task rows should not arm assistants that no longer belong to the space."""

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    space = Space(
        name="Restricted Room",
        description="Restricted room workspace for revoked membership tests.",
        owner_user_id=PRIMARY_USER_ID,
        status="active",
    )
    dbsession.add(space)
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
        context=f"Spaces/{space.space_id}/Tasks",
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    executor_activation_context = (
        task_machine_state_service.build_task_activation_context_name(
            _assistant_tasks_context(
                user_id=PRIMARY_USER_ID,
                assistant_id=assistant.agent_id,
            ),
        )
    )
    activations = await _get_context_logs(
        client,
        context_name=executor_activation_context,
    )
    assert all(log["entries"]["task_id"] != 112 for log in activations)
    assert materialization_calls == []


@pytest.mark.anyio
async def test_deleting_space_does_not_project_activation(
    client: AsyncClient,
    dbsession,
    materialization_calls,
):
    """Deleting spaces stop arming new scheduled work for member assistants."""

    await _ensure_task_machine_project(client)
    assistant = _make_assistant(dbsession, user_id=PRIMARY_USER_ID)
    space = _make_space_member(dbsession, assistant=assistant)
    space.status = "deleting"
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
        context=f"Spaces/{space.space_id}/Tasks",
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    executor_activation_context = (
        task_machine_state_service.build_task_activation_context_name(
            _assistant_tasks_context(
                user_id=PRIMARY_USER_ID,
                assistant_id=assistant.agent_id,
            ),
        )
    )
    activations = await _get_context_logs(
        client,
        context_name=executor_activation_context,
    )
    assert all(log["entries"]["task_id"] != 113 for log in activations)
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

    operation, created = (
        task_machine_state_service.create_task_outbound_operation_if_absent(
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
    )

    assert operation is adopted_row
    assert created is False


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
