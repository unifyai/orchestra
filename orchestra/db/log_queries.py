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

from sqlalchemy import Select, and_, select, true

from orchestra.db.models.orchestra_models import LogEvent, LogEventContext


def log_event_context_join(le=LogEvent, lec=LogEventContext):
    """Canonical, partition-pruning join condition between log_event and its
    context association.

    Always carries ``project_id`` into the join (``lec.project_id == le.project_id``)
    in addition to the id equality, so that constraining either side's
    ``project_id`` prunes BOTH partitioned tables. Use everywhere instead of the
    bare ``lec.log_event_id == le.id`` join. ``le`` / ``lec`` may be aliases.
    """
    return and_(
        lec.log_event_id == le.id,
        lec.project_id == le.project_id,
    )


def project_scope(log_event_alias, project_id):
    """A ``WHERE`` term pinning a ``log_event`` scan to ``project_id`` (prunes the
    ``LIST(project_id)`` partition); ``TRUE`` when ``project_id`` is ``None`` so it
    can be applied unconditionally. Use on any standalone ``log_event`` scan that
    is filtered by id / context but not yet by project."""
    if project_id is None:
        return true()
    return log_event_alias.project_id == project_id


def embedding_scope(embedding_alias, project_id):
    """A ``WHERE`` term pinning an ``embedding`` scan to ``project_id`` (prunes the
    ``LIST(project_id)`` partition); ``TRUE`` when ``project_id`` is ``None`` so it
    can be applied unconditionally. ``embedding`` is partitioned like log_event and
    fans out the same way when scanned by ``ref_id`` / ``key`` alone."""
    if project_id is None:
        return true()
    return embedding_alias.project_id == project_id


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
