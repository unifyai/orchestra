"""A repeating series whose worker crashed pre-dispatch must self-heal.

Recurrence advances when an occurrence is marked running. A pod that dies
before reaching that point — an image pull failure, an OOM at startup, a
SIGKILL during boot — leaves the definition armed with no open occurrence
and nothing to fire one, and the series halts silently. Releasing the stuck
run is the moment Orchestra knows the worker is gone, so the projection
advances the repeat rule itself and mints the next future slot. These tests
pin that behavior and its guard rails: a consumed series advances even with
a run in flight, and a non-repeating or exhausted definition still yields
nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import LogEvent, Project
from orchestra.services import task_machine_state_service as service
from orchestra.services.task_machine_state_service import (
    KEEP_CURRENT_HEAD,
    release_stuck_task_executions,
)
from orchestra.services.task_repetition import (
    RepeatPattern,
    deterministic_jitter_seconds,
)
from orchestra.tests.test_tasks.test_trigger_task import _auth_user_id
from orchestra.tests.utils import HEADERS

_TEN_MINUTES = [{"frequency": "minutely", "interval": 10}]


def _definition_row(
    repeat: list[dict] | None = _TEN_MINUTES,
    start_at: str = "2026-07-29T22:00:00+00:00",
) -> service._TaskRow:
    data = {
        "task_id": 12,
        "name": "Ten minute tick",
        "description": "Recurring operator tick.",
        "enabled": True,
        "schedule": {"start_at": start_at},
        "assistant_id": "42",
    }
    if repeat is not None:
        data["repeat"] = repeat
    return service._TaskRow(
        log_event_id=555,
        data=data,
        updated_at=None,
        created_at=None,
    )


def _project(
    row: service._TaskRow,
    *,
    latest: datetime | None,
):
    with (
        patch.object(service, "_open_execution_scheduled_for", return_value=None),
        patch.object(service, "_latest_ledger_occurrence", return_value=latest),
    ):
        return service._project_execution_payload(
            row=row,
            wake="scheduled",
            tasks_context_name="1/42/Tasks",
            destination=None,
            session=MagicMock(),
            project_id=1,
        )


def test_a_halted_repeating_series_mints_a_future_head() -> None:
    consumed = datetime.now(timezone.utc) - timedelta(minutes=37)

    payload = _project(_definition_row(), latest=consumed)

    assert payload is not KEEP_CURRENT_HEAD
    assert payload["state"] == "scheduled"
    minted = datetime.fromisoformat(payload["scheduled_for"])
    assert minted > datetime.now(timezone.utc)
    # The minted slot stays on the series grid anchored by the consumed one.
    assert (minted - consumed).total_seconds() % 600 == 0


def test_a_month_long_outage_still_mints_a_future_head() -> None:
    """Outage length must not bound the self-heal.

    Dense frequencies fast-forward to the present instead of stepping one slot
    at a time, so a series dead far longer than the projection's iteration
    bound (2048 slots — two weeks at ten minutes) still comes back.
    """

    consumed = datetime.now(timezone.utc) - timedelta(days=30, minutes=7)
    definition = _definition_row(
        start_at=(consumed - timedelta(days=1)).isoformat(),
    )

    payload = _project(definition, latest=consumed)

    assert payload is not KEEP_CURRENT_HEAD
    minted = datetime.fromisoformat(payload["scheduled_for"])
    assert minted > datetime.now(timezone.utc)
    assert minted - datetime.now(timezone.utc) <= timedelta(minutes=10)
    assert (minted - consumed).total_seconds() % 600 == 0


def test_a_run_in_flight_no_longer_defers_the_projection() -> None:
    """Projection advances a consumed series even while a run is in flight.

    This used to return KEEP_CURRENT_HEAD: the runtime projected the
    successor as part of starting a run, so deferring avoided racing it.
    Projection is owned here now — the relay is gone — so deferring would
    simply lose the occurrence.
    """

    consumed = datetime.now(timezone.utc) - timedelta(minutes=3)

    payload = _project(_definition_row(), latest=consumed)

    assert payload is not KEEP_CURRENT_HEAD
    assert payload["scheduled_for"] > consumed.isoformat()


def test_a_started_non_repeating_definition_keeps_its_head() -> None:
    consumed = datetime.now(timezone.utc) - timedelta(minutes=37)

    payload = _project(_definition_row(repeat=None), latest=consumed)

    assert payload is KEEP_CURRENT_HEAD


def test_an_exhausted_repeat_rule_keeps_its_head() -> None:
    consumed = datetime.now(timezone.utc) - timedelta(minutes=37)
    exhausted = [{"frequency": "minutely", "interval": 10, "count": 1}]

    payload = _project(_definition_row(repeat=exhausted), latest=consumed)

    assert payload is KEEP_CURRENT_HEAD


def test_a_fresh_series_still_projects_its_anchor() -> None:
    payload = _project(_definition_row(), latest=None)

    assert payload is not KEEP_CURRENT_HEAD
    assert payload["scheduled_for"] == "2026-07-29T22:00:00+00:00"
    assert payload["dispatch_offset_seconds"] == 0.0


def test_a_minted_head_carries_the_deterministic_jitter() -> None:
    jittered = [{"frequency": "minutely", "interval": 10, "jitter_seconds": 300}]
    consumed = datetime.now(timezone.utc) - timedelta(minutes=37)

    payload = _project(_definition_row(repeat=jittered), latest=consumed)

    assert payload is not KEEP_CURRENT_HEAD
    slot = datetime.fromisoformat(payload["scheduled_for"])
    expected = deterministic_jitter_seconds(
        task_id=12,
        slot=slot,
        patterns=[RepeatPattern.model_validate(p) for p in jittered],
    )
    assert payload["dispatch_offset_seconds"] == expected
    assert 0.0 < payload["dispatch_offset_seconds"] <= 300.0


@pytest.mark.anyio
async def test_releasing_a_crashed_run_leaves_a_future_scheduled_head(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production shape: a ten-minute repeat halted by a pre-dispatch crash.

    The task's only occurrence is consumed (its run died as ``running``) and
    no successor was ever projected. Releasing the run must terminalize it and
    leave exactly one open occurrence with a strictly future slot on the
    series grid, so the schedule survives the crash without an operator.
    """

    # Hermetic like CI: no comms/adapters backend to wake or sync against.
    monkeypatch.delenv("UNITY_COMMS_URL", raising=False)
    import orchestra.web.api.assistant.views as assistant_views

    monkeypatch.setattr(
        assistant_views,
        "comms_explicitly_configured",
        lambda: False,
    )

    created_assistant = await client.post(
        "/v0/assistant",
        json={"first_name": "Crash", "surname": "Loop", "create_infra": False},
        headers=HEADERS,
    )
    assert created_assistant.status_code == status.HTTP_200_OK
    agent_id = int(created_assistant.json()["info"]["agent_id"])

    anchor = datetime.now(timezone.utc) - timedelta(minutes=30)
    created_task = await client.post(
        f"/v0/assistants/{agent_id}/tasks",
        json={
            "name": "Ten minute tick",
            "description": "Recurring operator tick.",
            "schedule": {"start_at": anchor.isoformat()},
        },
        headers=HEADERS,
    )
    assert created_task.status_code == status.HTTP_201_CREATED
    source_task_log_id = int(created_task.json()["info"]["log_event_id"])

    project = (
        dbsession.query(Project)
        .filter(
            Project.user_id == _auth_user_id(),
            Project.organization_id.is_(None),
            Project.name == "Assistants",
        )
        .one()
    )

    def _executions():
        return (
            dbsession.query(LogEvent)
            .filter(
                LogEvent.project_id == project.id,
                LogEvent.data["source_task_log_id"].astext == str(source_task_log_id),
            )
            .all()
        )

    # The repeat rule is authored by the runtime, outside the typed API.
    definition = (
        dbsession.query(LogEvent)
        .filter(
            LogEvent.project_id == project.id,
            LogEvent.id == source_task_log_id,
        )
        .one()
    )
    definition.data = {**definition.data, "repeat": _TEN_MINUTES}

    # Dispatch fired the projected head and the pod died before executing.
    (head,) = _executions()
    assert head.data["state"] == "scheduled"
    consumed_slot = head.data["scheduled_for"]
    head.data = {**head.data, "state": "running"}
    dbsession.commit()

    result = release_stuck_task_executions(
        dbsession,
        project_id=project.id,
        source_task_log_id=source_task_log_id,
        info="pod died before dispatch",
    )
    dbsession.commit()

    assert result["updated"] is True
    assert result["reprojected"] is True

    rows = _executions()
    released = [row for row in rows if row.data["state"] == "failed"]
    open_rows = [row for row in rows if row.data["state"] == "scheduled"]
    assert len(released) == 1
    assert released[0].data["scheduled_for"] == consumed_slot
    assert len(open_rows) == 1
    minted = datetime.fromisoformat(open_rows[0].data["scheduled_for"])
    assert minted > datetime.now(timezone.utc)
    assert (minted - anchor).total_seconds() % 600 == 0

    # The definition stays pure authored intent.
    dbsession.refresh(definition)
    assert definition.data["task_revision"] == 1
    assert "state" not in definition.data
