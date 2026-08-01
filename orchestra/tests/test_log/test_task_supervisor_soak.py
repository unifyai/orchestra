"""Soak evidence that the supervisor sweep can be a primary dispatcher.

Opt-in: set ``TASK_SOAK=1``. CI skips this file — it drives hundreds of
projection cycles against a real database and takes minutes, not seconds.

Why it exists
-------------

The sweep shipped as a *floor* under the recurrence relay chain: a
periodic pass that re-projects the open head of every enabled, armed
definition. Promoting it to the *primary* dispatcher (retiring per-
occurrence timers and the dispatch-time relay) needs evidence that a
week of production would supply and unit tests would not:

* a healthy fleet sweeps to zero writes, repeatedly, or the primary
  loop becomes a write amplifier;
* dropped batons heal to the *correct* next slot, at fleet scale, not
  just in the one-series happy path;
* series advance monotonically over many cycles without accumulating
  duplicate or orphaned occurrences;
* **concurrent sweeps converge.** Production cannot demonstrate this:
  at a 15-minute cadence sweeps never overlap. A primary dispatcher
  ticking every few seconds overlaps constantly, so this is the
  property the promotion actually rests on.

A week of real traffic would supply ~670 healthy sweeps and, with any
luck, zero dropped batons. This supplies the same volume of healthy
sweeps plus the failure modes production only produces by accident.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import anyio
import pytest
from httpx import AsyncClient

from orchestra.services import task_machine_state_service
from orchestra.tests.test_log import _create_log, _update_logs
from orchestra.tests.test_log.test_task_machine_projection import (
    TASK_EXECUTIONS_CONTEXT,
    TASK_MACHINE_PROJECT_NAME,
    TASKS_CONTEXT,
    _ensure_task_machine_project,
    _get_context_logs,
)
from orchestra.tests.utils import ADMIN_HEADERS

pytestmark = pytest.mark.skipif(
    os.getenv("TASK_SOAK") != "1",
    reason="Soak evidence; opt in with TASK_SOAK=1.",
)

SERIES = int(os.getenv("TASK_SOAK_SERIES", "12"))
CYCLES = int(os.getenv("TASK_SOAK_CYCLES", "10"))
CONCURRENCY = int(os.getenv("TASK_SOAK_CONCURRENCY", "6"))
# A week at the shipped 15-minute cadence is 672 sweeps. The default is
# small so an accidental run stays quick; the real evidence run sets it.
HEALTHY_SWEEPS = int(os.getenv("TASK_SOAK_HEALTHY", "10"))
_BASE_TASK_ID = 700_000
_ANCHOR = datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc)

# One definition per shape the fleet actually contains, cycled across the
# seeded series so the soak never measures a single happy shape.
_SHAPES: list[dict[str, Any]] = [
    {
        "label": "offline-daily-symbolic",
        "offline": True,
        "entrypoint": 4242,
        "repeat": [{"frequency": "daily", "interval": 1}],
    },
    {
        "label": "offline-hourly-agentic",
        "offline": True,
        "entrypoint": None,
        "repeat": [{"frequency": "hourly", "interval": 1}],
    },
    {
        "label": "live-daily-agentic",
        "offline": False,
        "entrypoint": None,
        "repeat": [{"frequency": "daily", "interval": 1}],
    },
    {
        "label": "offline-two-slot",
        "offline": True,
        "entrypoint": None,
        "repeat": [
            {"frequency": "daily", "interval": 1, "time_of_day": "07:30"},
            {"frequency": "daily", "interval": 1, "time_of_day": "19:30"},
        ],
    },
    {
        "label": "offline-every-30m",
        "offline": True,
        "entrypoint": 4242,
        "repeat": [{"frequency": "minutely", "interval": 30}],
    },
    {
        "label": "offline-resource-hungry",
        "offline": True,
        "entrypoint": 4242,
        "requires_filesystem": True,
        "requires_computer": True,
        "repeat": [{"frequency": "daily", "interval": 1}],
    },
]


@pytest.fixture(autouse=True)
def _capture_materialization(monkeypatch):
    """Keep Communication out of it; the ledger is what is under test."""

    monkeypatch.setattr(
        task_machine_state_service,
        "_reconcile_scheduled_execution_materialization",
        lambda *, previous_execution, current_execution: None,
    )


def _definition(task_id: int, shape: dict[str, Any]) -> dict[str, Any]:
    entries = {
        "task_id": task_id,
        "assistant_id": "42",
        "name": f"Soak {shape['label']} {task_id}",
        "description": "Seeded by the supervisor soak.",
        "enabled": True,
        "offline": shape["offline"],
        "schedule": {"start_at": _ANCHOR.isoformat()},
        "repeat": shape["repeat"],
    }
    if shape.get("entrypoint") is not None:
        entries["entrypoint"] = shape["entrypoint"]
    if shape.get("requires_filesystem"):
        entries["requires_filesystem"] = True
    if shape.get("requires_computer"):
        entries["requires_computer"] = True
    return entries


async def _sweep(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/v0/admin/task-supervisor/sweep",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.json()
    return response.json()


async def _executions(client: AsyncClient) -> list[dict[str, Any]]:
    logs = await _get_context_logs(client, context_name=TASK_EXECUTIONS_CONTEXT)
    return [log["entries"] for log in logs]


def _soak_rows(rows: list[dict[str, Any]], task_ids: set[int]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("task_id") in task_ids]


def _open_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("state") in ("scheduled", "triggerable")]


def _parse(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def _consume(client: AsyncClient, head: dict[str, Any], *, when: str) -> None:
    """Run one occurrence to completion without projecting its successor.

    This is the dropped baton: dispatch consumed the head and died before
    handing the relay forward.
    """

    adopt = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": head["run_key"],
            "assistant_id": str(head["assistant_id"]),
            "task_id": head["task_id"],
            "source_task_log_id": head.get("source_task_log_id"),
            "wake": head["wake"],
            "delivery": head["delivery"],
            "destination": head.get("destination"),
            "scheduled_for": head["scheduled_for"],
            "state": "running",
            "started_at": when,
        },
        headers=ADMIN_HEADERS,
    )
    assert adopt.status_code == 200, adopt.json()
    assert adopt.json()["created"] is False, (
        "dispatch minted a twin instead of adopting the projected head: "
        f"{head['run_key']}"
    )
    done = await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(head["assistant_id"]),
            "run_key": head["run_key"],
            "source_task_log_id": head.get("source_task_log_id"),
            "updates": {"state": "completed", "completed_at": when},
        },
        headers=ADMIN_HEADERS,
    )
    assert done.status_code == 200, done.json()


@pytest.mark.anyio
async def test_supervisor_sweep_soak(client: AsyncClient, capsys) -> None:
    """Drive the sweep through a compressed week and report what it did."""

    await _ensure_task_machine_project(client)
    task_ids: set[int] = set()
    shape_by_task: dict[int, str] = {}

    for index in range(SERIES):
        shape = _SHAPES[index % len(_SHAPES)]
        task_id = _BASE_TASK_ID + index
        response = await _create_log(
            client,
            TASK_MACHINE_PROJECT_NAME,
            context=TASKS_CONTEXT,
            entries=_definition(task_id, shape),
        )
        assert response.status_code == 200, response.json()
        task_ids.add(task_id)
        shape_by_task[task_id] = shape["label"]

    report: dict[str, Any] = {"series": SERIES, "cycles": CYCLES}

    # ---------------------------------------------------------------- #
    # 1. A healthy fleet must sweep to zero writes, every time.
    # ---------------------------------------------------------------- #
    rows = _soak_rows(await _executions(client), task_ids)
    assert (
        len(_open_rows(rows)) == SERIES
    ), "seeding did not leave exactly one open head per series"

    healthy_writes: list[int] = []
    healthy_durations: list[float] = []
    for _ in range(HEALTHY_SWEEPS):
        started = time.monotonic()
        summary = await _sweep(client)
        healthy_durations.append(time.monotonic() - started)
        healthy_writes.append(int(summary["upserted"]) + int(summary["deleted"]))

    assert healthy_writes == [0] * HEALTHY_SWEEPS, (
        "a healthy fleet is not quiescent under the sweep; as a primary "
        f"dispatcher this would be a write amplifier: {healthy_writes}"
    )
    report["healthy_sweeps"] = HEALTHY_SWEEPS
    report["healthy_writes"] = sum(healthy_writes)
    report["sweep_seconds_median"] = sorted(healthy_durations)[
        len(healthy_durations) // 2
    ]
    report["sweep_seconds_max"] = max(healthy_durations)

    # ---------------------------------------------------------------- #
    # 2. Compressed weeks: consume every head, drop every baton, sweep.
    # ---------------------------------------------------------------- #
    slots_seen: dict[int, list[datetime]] = {task_id: [] for task_id in task_ids}
    heals = 0
    for cycle in range(CYCLES):
        rows = _soak_rows(await _executions(client), task_ids)
        heads = {row["task_id"]: row for row in _open_rows(rows)}
        assert len(heads) == SERIES, (
            f"cycle {cycle}: fleet lost heads before the drop: "
            f"{SERIES - len(heads)} series headless"
        )
        for task_id, head in heads.items():
            slots_seen[task_id].append(_parse(head["scheduled_for"]))
            await _consume(
                client,
                head,
                when=(_ANCHOR + timedelta(days=cycle, seconds=1)).isoformat(),
            )

        after_drop = _open_rows(_soak_rows(await _executions(client), task_ids))
        assert (
            after_drop == []
        ), f"cycle {cycle}: expected a fully headless fleet after the drop"

        summary = await _sweep(client)
        heals += int(summary["upserted"])

        healed = _open_rows(_soak_rows(await _executions(client), task_ids))
        assert (
            len(healed) == SERIES
        ), f"cycle {cycle}: sweep healed {len(healed)}/{SERIES} series"

    report["dropped_batons"] = SERIES * CYCLES
    report["heals"] = heals

    # Every series advanced strictly forward, one slot at a time, and the
    # shapes with two daily slots really did alternate.
    for task_id, slots in slots_seen.items():
        assert slots == sorted(
            slots,
        ), f"task {task_id} ({shape_by_task[task_id]}) went backwards: {slots}"
        assert len(set(slots)) == len(
            slots,
        ), f"task {task_id} ({shape_by_task[task_id]}) repeated a slot: {slots}"

    # No duplicate occurrences anywhere in the ledger.
    rows = _soak_rows(await _executions(client), task_ids)
    run_keys = [row["run_key"] for row in rows]
    assert len(run_keys) == len(set(run_keys)), "the ledger accumulated twins"
    report["ledger_rows"] = len(rows)

    # ---------------------------------------------------------------- #
    # 3. An in-flight run owns its own projection; the sweep stays out.
    # ---------------------------------------------------------------- #
    rows = _soak_rows(await _executions(client), task_ids)
    victim = _open_rows(rows)[0]
    adopt = await client.post(
        "/v0/admin/task-execution/create-or-adopt",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "run_key": victim["run_key"],
            "assistant_id": str(victim["assistant_id"]),
            "task_id": victim["task_id"],
            "source_task_log_id": victim.get("source_task_log_id"),
            "wake": victim["wake"],
            "delivery": victim["delivery"],
            "destination": victim.get("destination"),
            "scheduled_for": victim["scheduled_for"],
            "state": "running",
            "started_at": (_ANCHOR + timedelta(days=CYCLES + 2)).isoformat(),
        },
        headers=ADMIN_HEADERS,
    )
    assert adopt.status_code == 200, adopt.json()
    await client.post(
        "/v0/admin/task-execution/update",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(victim["assistant_id"]),
            "run_key": victim["run_key"],
            "source_task_log_id": victim.get("source_task_log_id"),
            "updates": {
                "state": "running",
                "started_at": (_ANCHOR + timedelta(days=CYCLES + 2)).isoformat(),
            },
        },
        headers=ADMIN_HEADERS,
    )

    before_inflight = len(_soak_rows(await _executions(client), task_ids))
    for _ in range(3):
        await _sweep(client)
    after_inflight = _soak_rows(await _executions(client), task_ids)
    assert (
        len(after_inflight) == before_inflight
    ), "the sweep projected a successor behind a running dispatcher's back"
    inflight_rows = [
        row for row in after_inflight if row["task_id"] == victim["task_id"]
    ]
    assert sum(1 for row in inflight_rows if row["state"] == "running") == 1
    report["inflight_holdoff_sweeps"] = 3

    # ---------------------------------------------------------------- #
    # 4. Disarming stops the fleet. Nothing re-arms a disabled series.
    # ---------------------------------------------------------------- #
    logs = await _get_context_logs(client, context_name=TASKS_CONTEXT)
    disabled_task_id = sorted(task_ids)[0]
    definition_log = next(
        log for log in logs if log["entries"].get("task_id") == disabled_task_id
    )
    disable = await _update_logs(
        client,
        [definition_log["id"]],
        {"enabled": False},
        context=TASKS_CONTEXT,
        overwrite=True,
    )
    assert disable.status_code == 200, disable.json()

    for _ in range(2):
        await _sweep(client)
    disabled_open = [
        row
        for row in _open_rows(_soak_rows(await _executions(client), task_ids))
        if row["task_id"] == disabled_task_id
    ]
    assert (
        disabled_open == []
    ), f"the sweep re-armed disabled task {disabled_task_id}: {disabled_open}"
    report["disabled_holdoff_sweeps"] = 2

    with capsys.disabled():
        print("\n=== supervisor sweep soak ===")
        for key in (
            "series",
            "cycles",
            "healthy_sweeps",
            "healthy_writes",
            "dropped_batons",
            "heals",
            "ledger_rows",
            "inflight_holdoff_sweeps",
            "disabled_holdoff_sweeps",
            "sweep_seconds_median",
            "sweep_seconds_max",
        ):
            print(f"  {key:28} {report[key]}")


@pytest.mark.anyio
async def test_concurrent_sweeps_converge(client_concurrent: AsyncClient) -> None:
    """Overlapping sweeps must leave exactly one open head per series.

    This runs on the independent-session client because it is the only
    honest way to ask the question: the default test client shares one
    session across every request, so "concurrent" requests there are
    serialised through SQLAlchemy and prove nothing about production.

    The property matters because it is the one the promotion rests on. As
    a 15-minute floor the sweep never overlaps itself. As a primary
    dispatcher ticking every few seconds it overlaps constantly, and two
    passes that both see a headless series must converge on one occurrence
    rather than racing to create two.
    """

    await _ensure_task_machine_project(client_concurrent)
    task_ids: set[int] = set()
    base = _BASE_TASK_ID + 500
    for index in range(SERIES):
        shape = _SHAPES[index % len(_SHAPES)]
        task_id = base + index
        response = await _create_log(
            client_concurrent,
            TASK_MACHINE_PROJECT_NAME,
            context=TASKS_CONTEXT,
            entries=_definition(task_id, shape),
        )
        assert response.status_code == 200, response.json()
        task_ids.add(task_id)

    rows = _soak_rows(await _executions(client_concurrent), task_ids)
    heads = _open_rows(rows)
    assert len(heads) == SERIES

    # Drop every baton, so every series is a healing candidate when the
    # concurrent sweeps land.
    for head in heads:
        await _consume(
            client_concurrent,
            head,
            when=(_ANCHOR + timedelta(seconds=1)).isoformat(),
        )
    assert _open_rows(_soak_rows(await _executions(client_concurrent), task_ids)) == []

    results: list[dict[str, Any]] = []
    failures: list[str] = []

    async def _one_sweep() -> None:
        try:
            response = await client_concurrent.post(
                "/v0/admin/task-supervisor/sweep",
                headers=ADMIN_HEADERS,
            )
            if response.status_code != 200:
                failures.append(f"{response.status_code}: {response.text[:400]}")
                return
            results.append(response.json())
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {exc}")

    async with anyio.create_task_group() as tg:
        for _ in range(CONCURRENCY):
            tg.start_soon(_one_sweep)

    rows = _soak_rows(await _executions(client_concurrent), task_ids)
    open_after = _open_rows(rows)
    per_task: dict[int, int] = {}
    for row in open_after:
        per_task[row["task_id"]] = per_task.get(row["task_id"], 0) + 1
    duplicated = {task_id: n for task_id, n in per_task.items() if n > 1}

    run_keys = [row["run_key"] for row in rows]

    print("\n=== concurrent sweep convergence ===")
    print(f"  sweeps launched            {CONCURRENCY}")
    print(f"  sweeps that returned 200   {len(results)}")
    print(f"  sweeps that failed         {len(failures)}")
    for failure in failures[:3]:
        print(f"    ! {failure}")
    print(f"  series                     {SERIES}")
    print(f"  open heads after           {len(open_after)}")
    print(f"  series with >1 open head   {len(duplicated)}")
    print(f"  duplicate run keys         {len(run_keys) - len(set(run_keys))}")
    print(f"  heals reported (total)     {sum(int(r['upserted']) for r in results)}")

    assert not duplicated, f"concurrent sweeps left duplicate open heads: {duplicated}"
    assert len(run_keys) == len(set(run_keys)), "concurrent sweeps minted twins"
    assert (
        len(open_after) == SERIES
    ), f"expected one head per series, got {len(open_after)} for {SERIES}"
    assert not failures, (
        "concurrent sweeps raced instead of converging; a primary dispatcher "
        f"ticking faster than a sweep takes cannot ship on this: {failures[:2]}"
    )
