"""Guarantee every enabled, armed task definition has its open head.

Recurrence is a relay chain: each occurrence's dispatch projects its
successor onto ``Tasks/Executions``. The chain is fast but droppable —
key drift, a lost write, a deploy-drain collision, or a crashed worker
can end a series silently, and every such incident has historically
needed an operator to notice and re-arm by hand.

This sweep is the floor under the relay chain. For every task-machine
project it finds each definition that is **enabled** and **armed**
(carries a ``schedule`` or a ``trigger``) and re-runs the standard
projection (:func:`sync_task_executions_for_task_ids`). Projection is
idempotent by design:

* a live open head is left untouched (``KEEP_CURRENT_HEAD``),
* a series with a run in flight is left to its dispatcher,
* a started series with no open occurrence is advanced from its repeat
  rule past the last ledger occurrence,
* a never-projected definition gets its anchor occurrence.

So a healthy fleet sweeps to zero writes, and a dropped baton self-heals
within one sweep interval instead of waiting for a human.

----------------------------------------------------------------------
Scheduling
----------------------------------------------------------------------

Runs **every 15 minutes** via Cloud Scheduler, mirroring the billing
routines:

  * Suggested job ``orchestra-production-task-supervisor-sweep`` in
    project ``gcp-project-saas`` / location ``us-central1``.
  * Schedule ``*/15 * * * *`` UTC.
  * POSTs to ``https://api.unify.ai/v0/admin/task-supervisor/sweep``
    with the static admin Bearer token.

Staging has no scheduled trigger — invoke on demand via the same admin
endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import (
    Context,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    is_task_surface_context_name,
    sync_task_executions_for_task_ids,
)
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


@dataclass
class TaskSupervisorSweepResult:
    """Summary returned by :func:`sweep_task_supervision`."""

    started_at: str = ""
    finished_at: str = ""
    projects_scanned: int = 0
    surfaces_scanned: int = 0
    definitions_scanned: int = 0
    upserted: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "projects_scanned": self.projects_scanned,
            "surfaces_scanned": self.surfaces_scanned,
            "definitions_scanned": self.definitions_scanned,
            "upserted": self.upserted,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "errors": self.errors,
        }


def sweep_task_supervision(
    session: Optional[Session] = None,
) -> TaskSupervisorSweepResult:
    """Re-project the open head for every enabled, armed task definition.

    Each surface is committed as its own transaction, so healing one
    tenant is durable before the next is attempted and no tenant's
    failure can discard or block another's repair.

    Args:
        session: DB session. A new one is created if ``None``.

    Returns:
        :class:`TaskSupervisorSweepResult` summary. ``upserted`` counts
        real projection writes, so a healthy fleet reports zero and any
        non-zero count is the number of dropped batons just healed;
        ``unchanged`` counts the heads that were already correct.
    """

    if session is not None:
        return _sweep_with_session(session)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as owned_session:
        return _sweep_with_session(owned_session)


def _armed_definition_ids_by_surface(
    session: Session,
    *,
    project_id: int,
) -> Dict[str, Set[int]]:
    """Map each task-surface context to its enabled, armed definition ids."""

    rows = (
        session.query(LogEvent.data, Context.name)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            Context.project_id == project_id,
            LogEvent.data.has_key("task_id"),
            # Missing `enabled` means enabled: the create path defaults it
            # to true, so only an explicit false disarms.
            func.coalesce(LogEvent.data.op("->>")("enabled"), "true") != "false",
        )
        .all()
    )

    by_surface: Dict[str, Set[int]] = {}
    for data, context_name in rows:
        if not is_task_surface_context_name(context_name):
            continue
        if not isinstance(data, dict):
            continue
        armed = isinstance(data.get("schedule"), dict) or isinstance(
            data.get("trigger"),
            dict,
        )
        if not armed:
            continue
        try:
            task_id = int(data.get("task_id"))
        except (TypeError, ValueError):
            continue
        by_surface.setdefault(str(context_name), set()).add(task_id)
    return by_surface


def _sweep_with_session(
    session: Session,
) -> TaskSupervisorSweepResult:
    result = TaskSupervisorSweepResult(
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    project_ids = [
        row[0]
        for row in session.query(Project.id)
        .filter(Project.name == TASK_MACHINE_PROJECT_NAME)
        .order_by(Project.id.asc())
        .all()
    ]

    for project_id in project_ids:
        result.projects_scanned += 1
        try:
            by_surface = _armed_definition_ids_by_surface(
                session,
                project_id=project_id,
            )
        except Exception as e:  # noqa: BLE001
            # Postgres marks the whole transaction aborted, so every later
            # statement on this session fails until it is rolled back.
            # Without this, one tenant's failure silently ends the sweep.
            session.rollback()
            msg = f"Failed to scan task surfaces for project {project_id}: {e}"
            logger.exception(msg)
            result.errors.append(msg)
            continue

        for context_name in sorted(by_surface):
            task_ids = sorted(by_surface[context_name])
            result.surfaces_scanned += 1
            result.definitions_scanned += len(task_ids)
            try:
                counts = sync_task_executions_for_task_ids(
                    session,
                    project_id,
                    task_ids,
                    tasks_context_name=context_name,
                )
                result.upserted += int(counts.get("upserted") or 0)
                result.deleted += int(counts.get("deleted") or 0)
                result.unchanged += int(counts.get("unchanged") or 0)
                # Commit per surface rather than once at the end: a single
                # transaction spanning every tenant holds its locks for the
                # whole sweep (which is how this started timing out) and
                # puts every repair at the mercy of the last one.
                session.commit()
            except Exception as e:  # noqa: BLE001
                # Per-surface isolation: one tenant's broken definitions
                # must not stop the sweep from healing everyone else. The
                # rollback is what makes that true — a caught exception
                # leaves the session aborted and unusable otherwise.
                session.rollback()
                msg = (
                    "Failed to re-project task surface "
                    f"{context_name!r} (project {project_id}): {e}"
                )
                logger.exception(msg)
                result.errors.append(msg)
                continue

    result.finished_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        {
            "message": "Task supervisor sweep complete",
            **result.to_dict(),
        },
    )
    return result
