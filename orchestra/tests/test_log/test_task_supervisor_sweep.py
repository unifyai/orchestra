"""Integration tests for the task supervisor sweep.

The sweep is the floor under the recurrence relay chain: a series whose
dispatch failed to project its successor (the dropped baton) must be
advanced by the next sweep, while healthy series, in-flight runs, and
disabled definitions are left exactly where they are.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from httpx import AsyncClient

from orchestra.services import task_machine_state_service
from orchestra.tests.test_log import _create_log
from orchestra.tests.test_log.test_task_machine_projection import (
    TASK_EXECUTIONS_CONTEXT,
    TASK_MACHINE_PROJECT_NAME,
    TASKS_CONTEXT,
    _ensure_task_machine_project,
    _get_context_logs,
    _scheduled_task_entries,
)
from orchestra.tests.utils import ADMIN_HEADERS


@pytest.fixture(autouse=True)
def _capture_materialization(monkeypatch):
    """Capture scheduled activation sync requests without hitting Communication."""

    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_execution_materialization",
        lambda *, previous_execution, current_execution: None,
    )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def _sweep(client: AsyncClient) -> dict:
    response = await client.post(
        "/v0/admin/task-supervisor/sweep",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.json()
    return response.json()


async def _rows_for_task(client: AsyncClient, task_id: int) -> list[dict]:
    executions = await _get_context_logs(
        client,
        context_name=TASK_EXECUTIONS_CONTEXT,
    )
    return [
        log["entries"] for log in executions if log["entries"].get("task_id") == task_id
    ]


@pytest.mark.anyio
async def test_sweep_heals_a_dropped_baton(client: AsyncClient) -> None:
    """A terminalized series with no open head is advanced by one sweep.

    This is the exact failure class the sweep exists for: dispatch consumed
    the head, crashed before projecting the successor, and the series died
    silently. Historically only an operator noticed.
    """

    await _ensure_task_machine_project(client)
    entries = _scheduled_task_entries(task_id=911)
    # The healing path parses the repeat rule to mint the successor slot, so
    # the seed must carry the real RepeatPattern shape.
    entries["repeat"] = [{"frequency": "daily", "interval": 1}]
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    projected = await _rows_for_task(client, 911)
    assert len(projected) == 1 and projected[0]["state"] == "scheduled"
    head = projected[0]

    # Dispatch adopts the head and starts running.
    adopt = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": head["run_key"],
            "assistant_id": str(head["assistant_id"]),
            "task_id": 911,
            "source_task_log_id": head.get("source_task_log_id"),
            "wake": head["wake"],
            "delivery": head["delivery"],
            "scheduled_for": head["scheduled_for"],
            "state": "running",
            "started_at": "2026-04-10T09:00:01+00:00",
        },
        headers=ADMIN_HEADERS,
    )
    assert adopt.status_code == 200 and adopt.json()["created"] is False

    running = await client.post(
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
    assert running.status_code == 200, running.json()

    # While the run is in flight its dispatcher owns projection: the sweep
    # must not mint a successor behind its back.
    summary = await _sweep(client)
    rows = await _rows_for_task(client, 911)
    assert (
        len(rows) == 1
    ), f"the sweep raced an in-flight run and minted a successor: {rows}"

    # The run terminalizes and the dispatcher crashes before projecting the
    # successor: the dropped baton.
    dropped = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(head["assistant_id"]),
            "run_key": head["run_key"],
            "source_task_log_id": head.get("source_task_log_id"),
            "updates": {
                "state": "completed",
                "completed_at": "2026-04-10T09:02:00+00:00",
            },
        },
        headers=ADMIN_HEADERS,
    )
    assert dropped.status_code == 200, dropped.json()
    assert all(
        row["state"] == "completed" for row in await _rows_for_task(client, 911)
    ), "precondition: no open occurrence remains"

    summary = await _sweep(client)
    assert summary["upserted"] >= 1, summary

    rows = await _rows_for_task(client, 911)
    open_rows = [row for row in rows if row["state"] == "scheduled"]
    assert len(open_rows) == 1, f"the sweep did not restore the head: {rows}"
    successor = open_rows[0]
    assert _parse_iso(successor["scheduled_for"]) > _parse_iso(
        head["scheduled_for"],
    ), "the healed head must advance past the consumed occurrence, not rebuild it"
    assert successor["run_key"] != head["run_key"]

    # Idempotence: a healthy series sweeps to zero writes and no new rows.
    before = len(await _rows_for_task(client, 911))
    summary = await _sweep(client)
    after = await _rows_for_task(client, 911)
    assert len(after) == before, f"a repeat sweep changed the series: {after}"
    assert len([row for row in after if row["state"] == "scheduled"]) == 1


@pytest.mark.anyio
async def test_sweep_leaves_disabled_definitions_alone(
    client: AsyncClient,
) -> None:
    """A disabled definition gets no head from the sweep — disarm means off."""

    await _ensure_task_machine_project(client)
    entries = _scheduled_task_entries(task_id=912)
    entries["enabled"] = False
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    await _sweep(client)
    open_rows = [
        row
        for row in await _rows_for_task(client, 912)
        if row["state"] in ("scheduled", "triggerable")
    ]
    assert open_rows == [], f"the sweep armed a disabled definition: {open_rows}"
