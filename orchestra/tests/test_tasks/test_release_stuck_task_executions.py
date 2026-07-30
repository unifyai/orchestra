"""Break-glass release terminalizes stuck runs, never a definition.

The previous implementation wrote ``failed`` onto the Tasks row with no check
for whether it repeats, so running the documented break-glass on a recurring
task disarmed it permanently. Run state lives on Tasks/Executions now, so
there is no definition state left for this to corrupt.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestra.services import task_machine_state_service as service
from orchestra.services.task_machine_state_service import release_stuck_task_executions


def _execution(run_key: str, state: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=hash(run_key) & 0xFFFF,
        data={
            "run_key": run_key,
            "state": state,
            "source_task_log_id": "555",
            "task_id": 12,
            "assistant_id": "42",
        },
        key_order={},
    )


def _session_returning(rows: list[SimpleNamespace]) -> MagicMock:
    session = MagicMock()
    query = session.query.return_value.filter.return_value
    query.filter.return_value = query
    query.all.return_value = rows
    return session


def test_running_executions_are_terminalized():
    session = _session_returning([_execution("run-a", "running")])

    with patch(
        "orchestra.services.task_machine_state_service._replace_log_payload",
    ) as replace:
        result = release_stuck_task_executions(
            session,
            project_id=1,
            source_task_log_id=555,
            info="worker gone",
        )

    assert result["updated"] is True
    assert result["released_run_keys"] == ["run-a"]
    written = replace.call_args.args[1]
    assert written["state"] == "failed"
    assert written["completed_at"]
    assert "worker gone" in str(written["error"])


def test_every_stuck_run_is_released():
    rows = [_execution("run-a", "running"), _execution("run-b", "running")]
    session = _session_returning(rows)

    with patch("orchestra.services.task_machine_state_service._replace_log_payload"):
        result = release_stuck_task_executions(
            session,
            project_id=1,
            source_task_log_id=555,
        )

    assert sorted(result["released_run_keys"]) == ["run-a", "run-b"]


def test_no_running_executions_is_a_noop():
    session = _session_returning([])

    with patch(
        "orchestra.services.task_machine_state_service._replace_log_payload",
    ) as replace:
        result = release_stuck_task_executions(
            session,
            project_id=1,
            source_task_log_id=555,
        )

    assert result["updated"] is False
    assert result["reason"] == "no_running_executions"
    assert result["released_run_keys"] == []
    replace.assert_not_called()


def test_release_never_writes_to_a_definition():
    """The regression this replaces: a recurring definition disarmed by hand."""

    session = _session_returning([_execution("run-a", "running")])

    with patch(
        "orchestra.services.task_machine_state_service._replace_log_payload",
    ) as replace:
        release_stuck_task_executions(
            session,
            project_id=1,
            source_task_log_id=555,
        )

    for call in replace.call_args_list:
        written = call.args[1]
        assert "status" not in written
        assert "enabled" not in written
        assert written.get("run_key"), "only execution rows may be written"


def test_release_is_scoped_to_one_run_when_given_a_run_key():
    """A finishing worker must not terminalize the occurrence that follows it.

    Recurrence projects the next occurrence at dispatch, so by the time a Job
    reaches a terminal condition its successor is often already running. An
    unscoped release from that worker killed the successor, and the series
    stopped advancing after a single healthy run.
    """

    session = _session_returning([_execution("run-a", "running")])

    release_stuck_task_executions(
        session,
        project_id=1,
        source_task_log_id=555,
        run_key="run-a",
    )

    scoped = session.query.return_value.filter.return_value
    assert scoped.filter.called, "run_key must narrow the query to one run"


def test_release_without_a_run_key_stays_definition_wide():
    """The break-glass keeps its original reach when no run is named."""

    session = _session_returning([_execution("run-a", "running")])

    release_stuck_task_executions(
        session,
        project_id=1,
        source_task_log_id=555,
    )

    scoped = session.query.return_value.filter.return_value
    assert not scoped.filter.called


def test_release_reprojects_the_head_so_a_crash_costs_one_occurrence() -> None:
    """A worker that died before projecting its successor must not end the series.

    Recurrence is computed in Unify at dispatch, so a pod killed before it got
    there leaves the definition armed with no open occurrence and nothing to fire
    one — a ten-minute tick sits dead until someone nudges it by hand. Releasing
    the run is the moment we know it is over, so the head is re-projected there.
    """

    session = _session_returning([_execution("run-a", "running")])

    with (
        patch.object(service, "_replace_log_payload"),
        patch.object(service, "resolve_tasks_context_name", return_value="1/42/Tasks"),
        patch.object(
            service,
            "sync_task_executions_for_task_ids",
            return_value={"upserted": 1, "deleted": 0},
        ) as sync,
    ):
        result = release_stuck_task_executions(
            session,
            project_id=1,
            source_task_log_id=555,
        )

    assert result["reprojected"] is True
    assert sync.call_args.kwargs["task_ids"] == [12]


def test_a_failed_reprojection_still_releases_the_run() -> None:
    """Releasing is the caller's contract; a headless series is recoverable."""

    session = _session_returning([_execution("run-a", "running")])

    with (
        patch.object(service, "_replace_log_payload"),
        patch.object(
            service,
            "resolve_tasks_context_name",
            side_effect=RuntimeError("orchestra unavailable"),
        ),
    ):
        result = release_stuck_task_executions(
            session,
            project_id=1,
            source_task_log_id=555,
        )

    assert result["updated"] is True
    assert result["released_run_keys"] == ["run-a"]
    assert result["reprojected"] is False
