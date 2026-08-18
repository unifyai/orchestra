"""Derived lifecycle view of a task definition.

``Tasks`` rows carry authored intent only: ``enabled`` says whether the
definition may fire, and its shape (schedule vs trigger) says how. Everything
about a *run* lives on ``Tasks/Executions``.

Nothing here is stored. Lifecycle is computed on read so that "what is this
task doing?" has one answer derived from one copy of each fact. The previous
model kept a mutable ``status`` on the definition that every concurrent run
wrote; the last writer won, and a single failed occurrence could disarm a
standing schedule permanently.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import LogEvent

__all__ = ["Lifecycle", "derive_task_lifecycle"]

_RUNNING_STATE = "running"
#: ``held`` is a run the assistant's runtime finished without performing its
#: effect because a verification it depended on failed or could not be
#: settled; the owner was told why. It is terminal like the other three.
_TERMINAL_STATES = ("completed", "failed", "cancelled", "held")


class Lifecycle(StrEnum):
    """What a definition is doing right now. Derived, never persisted."""

    disarmed = "disarmed"
    completed = "completed"
    running = "running"
    triggerable = "triggerable"
    scheduled = "scheduled"


def derive_task_lifecycle(
    session: Session,
    *,
    project_id: int,
    source_task_log_id: int,
    data: Mapping[str, Any],
) -> Lifecycle:
    """Project one definition's lifecycle from intent plus its run ledger.

    ``running`` outranks ``disarmed`` deliberately: disabling a task stops the
    *next* wake, it does not retroactively stop a run already in flight, and an
    operator watching a task they just paused should still see it finish.

    Telling ``completed`` from ``disarmed`` needs the ledger — a one-shot that
    ran and one an operator paused are both disarmed definitions, and only a
    terminal execution distinguishes them.
    """

    states = _execution_states(
        session,
        project_id=project_id,
        source_task_log_id=source_task_log_id,
    )
    if _RUNNING_STATE in states:
        return Lifecycle.running

    enabled = data.get("enabled")
    armed = True if enabled is None else bool(enabled)
    repeats = data.get("repeat") not in (None, [], {})
    has_trigger = data.get("trigger") not in (None, {})

    if not armed:
        one_shot = not repeats and not has_trigger
        if one_shot and any(state in states for state in _TERMINAL_STATES):
            return Lifecycle.completed
        return Lifecycle.disarmed
    return Lifecycle.triggerable if has_trigger else Lifecycle.scheduled


def _execution_states(
    session: Session,
    *,
    project_id: int,
    source_task_log_id: int,
) -> set[str]:
    """States of every execution belonging to one definition."""

    rows = (
        session.query(LogEvent)
        .filter(
            LogEvent.project_id == project_id,
            LogEvent.data["source_task_log_id"].astext == str(int(source_task_log_id)),
        )
        .all()
    )
    return {
        str((row.data or {}).get("state") or "")
        for row in rows
        if (row.data or {}).get("state")
    }
