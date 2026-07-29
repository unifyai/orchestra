"""A projected occurrence must be pending, future, and reach the delayed queue.

The recurring series advances by projecting the next occurrence at dispatch.
Two defects made that projection inert: the row was created already ``running``
(so predecessor and successor overlap-skipped each other forever), and nothing
materialized a Cloud Task for it (the definition-write sync cannot see a row
that deliberately writes no definition). These pin the service-side halves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from orchestra.services import task_machine_state_service as service


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def test_a_future_pending_row_is_in_the_future() -> None:
    due = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert service._occurrence_is_in_the_future({"scheduled_for": _iso(due)})


def test_jitter_counts_toward_the_dispatch_moment() -> None:
    due = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert service._occurrence_is_in_the_future(
        {"scheduled_for": _iso(due), "dispatch_offset_seconds": 120.0},
    )


def test_a_due_or_past_row_is_not_in_the_future() -> None:
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert not service._occurrence_is_in_the_future({"scheduled_for": _iso(due)})
    assert not service._occurrence_is_in_the_future({})


def test_snapshot_keeps_an_empty_revision_and_the_jitter() -> None:
    """The dispatcher rebuilds the run key from these fields at fire time.

    Unify-projected occurrences digest revision "" into the key, so the empty
    revision is an identity, not a gap — replacing it would mint a second
    execution for the same occurrence. The jitter must ride along or the
    delayed task fires on the exact slot boundary.
    """

    snapshot = service._scheduled_execution_snapshot(
        {
            "wake": "scheduled",
            "assistant_id": "1406",
            "task_id": 12,
            "scheduled_for": "2026-07-29T17:40:00+00:00",
            "dispatch_offset_seconds": 72.99,
            "delivery": "offline",
        },
    )
    assert snapshot is not None
    assert snapshot["revision"] == ""
    assert snapshot["dispatch_offset_seconds"] == 72.99


class _CreatedRow(SimpleNamespace):
    pass


def _machine_row(payload: dict) -> SimpleNamespace:
    row = _CreatedRow(id=901, data=dict(payload), key_order={})
    return SimpleNamespace(row=row, created=True)


def _run_create(payload: dict, *, created: bool = True):
    """Drive create_task_run_if_absent with its persistence faked out."""

    outcome = _machine_row(payload)
    outcome.created = created
    captured: list[tuple[dict | None, dict | None]] = []
    with (
        patch.object(service, "resolve_tasks_context_name", return_value="Tasks"),
        patch.object(
            service,
            "ensure_task_machine_contexts",
            return_value=SimpleNamespace(
                executions_context_id=1,
                outbound_operations_context_id=2,
            ),
        ),
        patch.object(service, "_get_machine_row_by_unique_field", return_value=None),
        patch.object(
            service,
            "_migrate_legacy_machine_row_if_present",
            return_value=None,
        ),
        patch.object(service, "_upsert_machine_row", return_value=outcome),
        patch.object(service, "_replace_log_payload"),
        patch.object(
            service,
            "_reconcile_scheduled_execution_materialization",
            side_effect=lambda **kwargs: captured.append(
                (kwargs["previous_execution"], kwargs["current_execution"]),
            ),
        ),
    ):
        from unittest.mock import MagicMock

        service.create_task_run_if_absent(
            session=MagicMock(),
            project_id=1,
            payload=dict(payload),
        )
    return captured


def _future_projection_payload() -> dict:
    return {
        "run_key": "offline:scheduled:1406:team-11:12:e3b0c44298fc:20260730T101000Z",
        "assistant_id": "1406",
        "task_id": 12,
        "wake": "scheduled",
        "delivery": "offline",
        "state": "scheduled",
        "scheduled_for": _iso(datetime.now(timezone.utc) + timedelta(minutes=10)),
        "run_id": 901,
    }


def test_created_future_projection_materializes_once() -> None:
    captured = _run_create(_future_projection_payload())
    assert len(captured) == 1
    previous, current = captured[0]
    assert previous is None
    assert current["task_id"] == 12


def test_adopted_row_does_not_rematerialize() -> None:
    assert _run_create(_future_projection_payload(), created=False) == []


def test_running_creation_does_not_materialize() -> None:
    payload = {**_future_projection_payload(), "state": "running"}
    assert _run_create(payload) == []


def test_due_creation_does_not_materialize() -> None:
    """A dispatch-time create is being fired right now; a delayed task for it
    would fire again immediately."""

    payload = {
        **_future_projection_payload(),
        "scheduled_for": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
    }
    assert _run_create(payload) == []
