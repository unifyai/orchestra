"""Guarantee every enabled, armed task definition has its open head.

Recurrence advances when an occurrence is marked ``running``: that
transition projects the successor onto ``Tasks/Executions``, inside the
component that owns the repeat rule. So a series loses its head only when
that transition never happens — an occurrence terminalized without ever
running, or a projection that raised on the way. Both are silent, and
before this sweep existed they needed an operator to notice and re-arm by
hand.

This sweep is the floor under that. For every task-machine
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

Runs **every 15 minutes** via Cloud Scheduler:

  * Job ``orchestra-production-task-supervisor-sweep`` in project
    ``gcp-project-saas`` / location ``us-central1``.
  * Schedule ``*/15 * * * *`` UTC.
  * POSTs to ``https://api.unify.ai/v0/admin/task-supervisor/sweep`` as
    ``task-supervisor-sweep@gcp-project-saas``, a dedicated identity holding
    no project roles, matched against ``CLOUD_SCHEDULER_SERVICE_ACCOUNT``.
    It carried the org-wide admin key in a header until August 2026; the
    Cloud Scheduler API hands those back in plaintext to anyone who can
    read the job, so do not put it back.

Staging mirrors it as ``orchestra-staging-task-supervisor-sweep`` against
the staging host; both jobs are ensured idempotently by
``deploy/ensure_task_supervisor_sweep_scheduler.sh``. The same admin
endpoint also serves on-demand invocation.

The schedule only records a status code, which is why a pass that failed
across the fleet answers 5xx (see :attr:`TaskSupervisorSweepResult.status`).
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
    expire_missed_task_executions,
    is_task_surface_context_name,
    sync_task_executions_for_task_ids,
)
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


#: A pass that failed for most of what it walked did not do its job, however
#: many repairs it managed on the way. Below this, individual tenants are
#: broken and the sweep still ran, which is a different problem and a
#: different level: a job that goes red for one bad tenant gets ignored, and
#: an ignored alarm is worth less than no alarm.
_BROKEN_ERROR_SHARE = 0.5


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
    #: Occurrences whose moment passed with nothing running them. Counted
    #: separately from ``upserted`` because it is the one number here that
    #: reports lost work rather than routine repair: a fleet that expires
    #: occurrences every pass is dropping runs somewhere upstream.
    expired: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Whether this pass did its job: ``ok``, ``degraded`` or ``broken``.

        ``upserted`` cannot answer this on its own, which is the whole reason
        this exists. A healthy fleet repairs nothing, and so does a sweep that
        aborted on its first tenant — in August 2026 one returned
        ``projects_scanned: 1552, surfaces_scanned: 1, upserted: 0`` every
        fifteen minutes for two days and read as a fleet with nothing wrong.
        """

        if not self.errors:
            return "ok"
        walked = max(self.projects_scanned, 1)
        return (
            "broken" if len(self.errors) >= walked * _BROKEN_ERROR_SHARE else "degraded"
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "projects_scanned": self.projects_scanned,
            "surfaces_scanned": self.surfaces_scanned,
            "definitions_scanned": self.definitions_scanned,
            "upserted": self.upserted,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "expired": self.expired,
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
        # A repeat rule alone arms a series: recurring definitions often
        # carry no `schedule` at all, and skipping them here left exactly
        # the tasks most dependent on the sweep outside it.
        armed = (
            isinstance(data.get("schedule"), dict)
            or isinstance(data.get("trigger"), dict)
            or bool(data.get("repeat"))
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
                # Before re-projecting, not after: projection reads the
                # earliest open occurrence as the definition's head, so an
                # occurrence that fired and never started keeps that seat and
                # the pass writes nothing. Expiring it first is what leaves
                # the projection below with no head to find, so it mints the
                # next one.
                result.expired += expire_missed_task_executions(
                    session,
                    project_id=project_id,
                    task_ids=task_ids,
                    tasks_context_name=context_name,
                )
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
    payload = {"message": "Task supervisor sweep complete", **result.to_dict()}
    # Log at the level the outcome deserves, so a broken pass is not one more
    # INFO line among the healthy ones it looks identical to.
    if result.status == "broken":
        logger.error(payload)
    elif result.status == "degraded":
        logger.warning(payload)
    elif result.upserted:
        # Since projection moved onto the run-start transition, a repair is no
        # longer routine work — it means a series lost its head, so a run
        # started without minting its successor. The sweep catching it is the
        # floor doing its job, and every catch is a bug somewhere above.
        logger.warning(
            {
                **payload,
                "message": (
                    "Task supervisor sweep repaired series that lost their "
                    "head; run-start projection did not happen for them"
                ),
            },
        )
    else:
        logger.info(payload)
    return result
