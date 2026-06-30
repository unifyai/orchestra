"""Partition-safe query builders for the ``log_event`` family.

``log_event`` and ``log_event_context`` are ``LIST (project_id)``-partitioned.
A query that does not constrain ``project_id`` to a literal value cannot be
pruned by the planner, so it fans out across **every** tenant's partition
(an ``Append`` that scans each one). With large tenants promoted to their own
partitions this turns a single-partition lookup into a full multi-million-row
scan per call -- the root cause of the June 2026 production slowdown.

``project_id`` is *always* derivable at these call sites (a context belongs to
exactly one project), so it is made a **required** argument here: callers
physically cannot build an unscoped log query through this helper.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, select

from orchestra.db.models.orchestra_models import LogEvent, LogEventContext


def project_scoped_log_events(project_id: int, *columns) -> Select:
    """Base ``SELECT`` over ``log_event JOIN log_event_context`` pruned to one project.

    ``project_id`` is applied to both partitioned tables (and carried into the
    join) so the planner prunes to the single project's partition instead of
    fanning out across all of them. Pass the columns to select (defaults to the
    whole ``LogEvent`` entity); extend the returned ``Select`` with the usual
    ``.where()`` / ``.order_by()`` / ``.limit()`` (e.g. a ``context_id`` or a
    ``data``-field predicate).

    Raises ``ValueError`` if ``project_id`` is missing: there is no correct
    scenario for querying the partitioned log tables without it.
    """
    if project_id is None:
        raise ValueError(
            "project_id is required: log_event/log_event_context are "
            "LIST(project_id)-partitioned and an unscoped query fans out across "
            "every tenant's partition",
        )
    selected = columns or (LogEvent,)
    return (
        select(*selected)
        .join(
            LogEventContext,
            and_(
                LogEventContext.log_event_id == LogEvent.id,
                # Carry the partition key into the join so BOTH partitioned
                # tables prune to the same single partition.
                LogEventContext.project_id == LogEvent.project_id,
            ),
        )
        .where(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
        )
    )
