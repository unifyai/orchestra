"""Integration tests for the task supervisor sweep.

Projection rides the run-start transition, so a series only loses its head
when that transition never happens — an occurrence terminalized without
ever running. The sweep is the floor under that: it must advance such a
series, while healthy series, in-flight runs, and disabled definitions are
left exactly where they are.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_a_failing_surface_does_not_end_the_sweep(dbsession, monkeypatch) -> None:
    """One tenant's failure must not swallow every tenant behind it.

    Postgres marks a transaction aborted when a statement fails, so a
    caught exception leaves the session refusing every later statement
    with ``current transaction is aborted``. The first surface to raise
    therefore took the whole sweep with it — a production run across 1552
    projects healed nothing while returning HTTP 200 and a single error.

    This asserts the recovery (a rollback before continuing) rather than
    the cascade itself, because the cascade cannot be staged here: the
    test engine is built with ``isolation_level="AUTOCOMMIT"``, which
    gives every statement its own transaction and so cannot reproduce the
    very failure mode this guards. Asserting the rollback is what keeps
    that harness difference from hiding a regression.
    """

    from orchestra.db.models.core_models import Project
    from orchestra.routines import task_supervisor_sweep as sweep_module

    # The sweep iterates task-machine projects, so give it one of its own
    # rather than depending on another test having left one behind.
    dbsession.add(
        Project(name=TASK_MACHINE_PROJECT_NAME, user_id="sweep-isolation-probe"),
    )
    dbsession.flush()

    monkeypatch.setattr(
        sweep_module,
        "_armed_definition_ids_by_surface",
        lambda session, *, project_id: {"first/Tasks": {1}, "second/Tasks": {2}},
    )

    attempted: list[str] = []

    def _fail_the_first_surface(session, project_id, task_ids, **kwargs):
        attempted.append(str(kwargs.get("tasks_context_name")))
        if len(attempted) == 1:
            raise RuntimeError("projection blew up for this tenant")
        return {"upserted": 0, "deleted": 0, "unchanged": 1}

    monkeypatch.setattr(
        sweep_module,
        "sync_task_executions_for_task_ids",
        _fail_the_first_surface,
    )

    rollbacks: list[int] = []
    real_rollback = dbsession.rollback
    monkeypatch.setattr(
        dbsession,
        "rollback",
        lambda: (rollbacks.append(1), real_rollback())[1],
    )

    result = sweep_module._sweep_with_session(dbsession)

    assert attempted == [
        "first/Tasks",
        "second/Tasks",
    ], f"the sweep skipped a surface entirely: attempted={attempted}"
    assert rollbacks, (
        "the sweep continued on an aborted session without rolling back, "
        "which is what silently ended it in production"
    )
    assert len(result.errors) == 1 and "first/Tasks" in result.errors[0]
    assert result.unchanged == 1, "the surviving surface did no work"


class TestSweepStatus:
    """A pass has to say whether it worked, because its counts cannot.

    ``upserted`` is zero for a fleet with nothing to repair and zero for a
    sweep that aborted on its first tenant, which is how one ran every
    fifteen minutes for two days looking healthy while fixing nothing.
    """

    def _result(self, *, errors: int, projects: int) -> object:
        from orchestra.routines.task_supervisor_sweep import TaskSupervisorSweepResult

        return TaskSupervisorSweepResult(
            projects_scanned=projects,
            errors=[f"surface {index} blew up" for index in range(errors)],
        )

    def test_a_clean_pass_is_ok(self):
        assert self._result(errors=0, projects=1552).status == "ok"

    def test_one_broken_tenant_does_not_condemn_the_fleet(self):
        """A job that goes red for one bad tenant gets ignored within a week."""

        assert self._result(errors=1, projects=1552).status == "degraded"

    def test_the_august_cascade_reads_as_broken(self):
        """The shape that hid for two days: one lock timeout, then everyone."""

        assert self._result(errors=1551, projects=1552).status == "broken"

    def test_a_fleet_that_failed_entirely_is_broken_at_any_size(self):
        """Scale must not rescue a pass that did nothing.

        An earlier version kept an absolute floor alongside the share, which
        meant a small fleet failing every single tenant still read as merely
        degraded, and the endpoint answered 200.
        """

        assert self._result(errors=1, projects=1).status == "broken"
        assert self._result(errors=2, projects=3).status == "broken"


@pytest.mark.anyio
async def test_a_broken_sweep_does_not_report_success_to_its_scheduler(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Cloud Scheduler records the status code and nothing else.

    Answering 200 with the failures in the body is precisely how a sweep
    repaired nothing for two days while its schedule reported success on
    every one of those ticks.
    """

    await _ensure_task_machine_project(client)

    from orchestra.routines import task_supervisor_sweep as sweep_module

    def _explode(session, *, project_id):
        raise RuntimeError("scan blew up for this tenant")

    monkeypatch.setattr(
        sweep_module,
        "_armed_definition_ids_by_surface",
        _explode,
    )

    response = await client.post(
        "/v0/admin/task-supervisor/sweep",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 500, response.json()
    detail = response.json()["detail"]
    assert detail["status"] == "broken"
    assert detail["errors"], "the failures must survive into the response"


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


@pytest.mark.anyio
async def test_sweep_expires_an_occurrence_nothing_ever_ran(
    client: AsyncClient,
) -> None:
    """A head that fired and never started must not pin the series forever.

    Projection reads a definition's head as its earliest open occurrence,
    with no bound on age, so an occurrence nobody ran kept that seat: the
    definition had an open head, the sweep read it as healthy and wrote
    nothing, and no later occurrence was ever minted. Two workflows on one
    staging assistant stopped firing exactly this way, and the only visible
    trace was that their next slots never appeared.
    """

    await _ensure_task_machine_project(client)
    entries = _scheduled_task_entries(task_id=913)
    entries["repeat"] = [{"frequency": "daily", "interval": 1}]
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    # The seed's anchor is in the past, so the projected head is already a
    # moment that came and went with nothing running it.
    projected = await _rows_for_task(client, 913)
    assert len(projected) == 1
    head = projected[0]
    assert head["state"] == "scheduled"
    assert _parse_iso(head["scheduled_for"]) < datetime.now(timezone.utc)

    body = await _sweep(client)

    assert body["expired"] == 1
    rows = await _rows_for_task(client, 913)
    missed = [row for row in rows if row["run_key"] == head["run_key"]]
    assert len(missed) == 1
    # Kept and marked rather than deleted: "this never ran" is the answer to
    # why nothing arrived, and a deleted row answers nothing.
    assert missed[0]["state"] == "failed"
    assert "never started" in missed[0]["error"]
    assert missed[0]["completed_at"]

    # And with the seat vacated, the same pass minted the next occurrence.
    successors = [row for row in rows if row["state"] == "scheduled"]
    assert len(successors) == 1
    assert _parse_iso(successors[0]["scheduled_for"]) > datetime.now(timezone.utc)


@pytest.mark.anyio
async def test_sweep_leaves_an_occurrence_that_is_merely_recent_alone(
    client: AsyncClient,
) -> None:
    """Being past due is not the same as having been missed.

    A wake in flight has a queue hop, a pod cold start and manager init to
    get through before anything records a start. Expiring inside that window
    would cancel live runs, which is the one outcome worse than the stall
    this repair exists for.
    """

    await _ensure_task_machine_project(client)
    entries = _scheduled_task_entries(task_id=914)
    entries["repeat"] = [{"frequency": "daily", "interval": 1}]
    entries["schedule"] = {
        "start_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
    }
    response = await _create_log(
        client,
        TASK_MACHINE_PROJECT_NAME,
        context=TASKS_CONTEXT,
        entries=entries,
    )
    assert response.status_code == 200, response.json()

    body = await _sweep(client)

    assert body["expired"] == 0
    rows = await _rows_for_task(client, 914)
    assert [row["state"] for row in rows] == ["scheduled"]
