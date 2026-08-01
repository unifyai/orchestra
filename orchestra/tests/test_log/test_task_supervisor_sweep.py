"""Integration tests for the task supervisor sweep.

Projection rides the run-start transition, so a series only loses its head
when that transition never happens — an occurrence terminalized without
ever running. The sweep is the floor under that: it must advance such a
series, while healthy series, in-flight runs, and disabled definitions are
left exactly where they are.
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

    # Marking the run running is what makes its successor due, and Orchestra
    # projects it on that transition — no relay, no dispatcher bookkeeping.
    rows = await _rows_for_task(client, 911)
    successors = [row for row in rows if row["state"] == "scheduled"]
    assert (
        len(successors) == 1
    ), f"starting the run did not project its successor: {rows}"
    assert _parse_iso(successors[0]["scheduled_for"]) > _parse_iso(
        head["scheduled_for"],
    )

    # Sweeping alongside the in-flight run must converge, not duplicate.
    before = len(rows)
    await _sweep(client)
    assert (
        len(await _rows_for_task(client, 911)) == before
    ), "the sweep duplicated the successor an in-flight run had already projected"

    # Finish the run, then drive the series headless the only way that
    # remains: terminalize the projected successor without it ever starting,
    # so nothing projects behind it. That is the state the sweep exists for -
    # a projection that never landed, whatever the cause.
    for run_key in (head["run_key"], successors[0]["run_key"]):
        terminal = await client.post(
            "/v0/admin/task-execution/update",
            json={
                "project_name": TASK_MACHINE_PROJECT_NAME,
                "assistant_id": str(head["assistant_id"]),
                "run_key": run_key,
                "source_task_log_id": head.get("source_task_log_id"),
                "updates": {
                    "state": "completed",
                    "completed_at": "2026-04-10T09:02:00+00:00",
                },
            },
            headers=ADMIN_HEADERS,
        )
        assert terminal.status_code == 200, terminal.json()
    assert all(
        row["state"] == "completed" for row in await _rows_for_task(client, 911)
    ), "precondition: no open occurrence remains"

    summary = await _sweep(client)
    assert summary["upserted"] >= 1, summary

    rows = await _rows_for_task(client, 911)
    open_rows = [row for row in rows if row["state"] == "scheduled"]
    assert len(open_rows) == 1, f"the sweep did not restore the head: {rows}"
    healed = open_rows[0]
    assert _parse_iso(healed["scheduled_for"]) > _parse_iso(
        successors[0]["scheduled_for"],
    ), "the healed head must advance past the consumed occurrence, not rebuild it"
    assert healed["run_key"] not in {head["run_key"], successors[0]["run_key"]}

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


class TestAuthoredRevision:
    """The occurrence fingerprint tracks authored intent, not payload shape."""

    def _payload(self, **overrides) -> dict:
        base = {
            "assistant_id": "1406",
            "destination": "team:11",
            "task_id": 12,
            "source_task_log_id": 555,
            "wake": "scheduled",
            "delivery": "offline",
            "state": "scheduled",
            "scheduled_for": "2026-08-01T10:00:00+00:00",
            "entrypoint": 77,
            "max_runtime_seconds": 3600,
            "requires_filesystem": False,
            "requires_computer": False,
            "recurring": True,
            "trigger_medium": None,
            "trigger_from_contact_ids": None,
            "trigger_omit_contact_ids": None,
            "trigger_recurring": False,
            "interrupt": False,
            "task_name": "GTM SmartLead campaign runtime",
            "source_task_updated_at": "2026-08-01T09:00:00+00:00",
        }
        base.update(overrides)
        return base

    def test_projection_schema_changes_do_not_rekey_the_fleet(self):
        """Adding or dropping a projected column must be a non-event.

        Hashing the whole payload made every schema edit look like an
        authored edit: the column diet re-keyed every armed head in the
        fleet, and so did adding tags before it.
        """

        before = task_machine_state_service._authored_revision(self._payload())

        widened = self._payload()
        widened["some_new_projected_column"] = "added next month"
        narrowed = self._payload()
        del narrowed["source_task_updated_at"]

        assert task_machine_state_service._authored_revision(widened) == before
        assert task_machine_state_service._authored_revision(narrowed) == before

    def test_cosmetic_edits_do_not_retire_a_live_head(self):
        """A rename is the same run under a new label."""

        before = task_machine_state_service._authored_revision(self._payload())
        renamed = self._payload(task_name="Renamed but identical work")
        touched = self._payload(
            source_task_updated_at="2026-08-01T09:30:00+00:00",
        )

        assert task_machine_state_service._authored_revision(renamed) == before
        assert task_machine_state_service._authored_revision(touched) == before

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("scheduled_for", "2026-08-01T10:30:00+00:00"),
            ("entrypoint", 78),
            ("delivery", "live"),
            ("destination", "team:12"),
            ("requires_computer", True),
            ("max_runtime_seconds", 1800),
            ("recurring", False),
            ("trigger_medium", "api_message"),
            ("source_task_log_id", 556),
        ],
    )
    def test_authored_changes_mint_a_new_occurrence(self, field, value):
        before = task_machine_state_service._authored_revision(self._payload())
        after = task_machine_state_service._authored_revision(
            self._payload(**{field: value}),
        )
        assert (
            after != before
        ), f"changing {field} must retire the head and mint a new occurrence"


class TestBoundedWakeSummary:
    """A live wake needs to say what the work is, at a fixed cost."""

    def test_a_short_description_passes_through_intact(self):
        summary = task_machine_state_service._compact_task_summary(
            "Quietly start this work when it becomes due.",
            fallback="Integration scheduled task 7",
        )
        assert summary == "Quietly start this work when it becomes due."

    def test_a_long_description_is_bounded_not_dropped(self):
        """The diet's objection was unbounded copies, not summaries."""

        description = " ".join(f"clause{n}" for n in range(200))
        summary = task_machine_state_service._compact_task_summary(
            description,
            fallback="Some task",
        )
        assert len(summary) <= 240, f"summary ran to {len(summary)} chars"
        assert summary.endswith("...")
        assert summary.startswith("clause0 clause1")

    def test_whitespace_is_collapsed(self):
        summary = task_machine_state_service._compact_task_summary(
            "Send   the\n\n  daily   digest.",
            fallback="x",
        )
        assert summary == "Send the daily digest."

    @pytest.mark.parametrize("empty", [None, "", "   ", "\n\t"])
    def test_an_empty_description_falls_back_to_the_title(self, empty):
        summary = task_machine_state_service._compact_task_summary(
            empty,
            fallback="GTM SmartLead campaign runtime",
        )
        assert summary == "GTM SmartLead campaign runtime"
