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


def log_event_context_join(le=LogEvent, lec=LogEventContext, owner_key=None):
    """Canonical, partition-pruning join condition between log_event and its
    context association.

    Always carries ``project_id`` into the join (``lec.project_id == le.project_id``)
    in addition to the id equality, so that constraining either side's
    ``project_id`` prunes BOTH partitioned tables. Use everywhere instead of the
    bare ``lec.log_event_id == le.id`` join. ``le`` / ``lec`` may be aliases.

    Pass a non-``None`` ``owner_key`` to *also* carry the owner sub-partition key
    into the join (``lec.owner_key == le.owner_key``). An association always lands
    in its log's owner sub-partition, so this equijoin is structurally always true
    and lets a literal ``owner_key`` predicate on one side prune the other's owner
    sub-partition too. It is opt-in so non-owner-scoped callers are unchanged.
    """
    conds = [
        lec.log_event_id == le.id,
        lec.project_id == le.project_id,
    ]
    if owner_key is not None:
        conds.append(lec.owner_key == le.owner_key)
    return and_(*conds)


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


def owner_scope_clause(alias, owner_key):
    """A ``WHERE`` term pinning a ``log_event`` / ``log_event_context`` /
    ``embedding`` scan to a single ``owner_key`` (prunes the per-owner
    sub-partition within an owner-sub-partitioned project, i.e. the shared
    Assistants project); ``TRUE`` when ``owner_key`` is ``None`` so it can be
    applied unconditionally.

    Only pass an ``owner_key`` for genuinely single-owner (assistant/team)
    contexts -- see ``orchestra.db.scope.single_owner_key``. Aggregation/system
    contexts are heterogeneous and must NOT be pinned to one owner.
    """
    if owner_key is None:
        return true()
    return alias.owner_key == owner_key


def project_scoped_log_events(project_id: int, *columns, owner_key=None) -> Select:
    """Base ``SELECT`` over ``log_event JOIN log_event_context`` pruned to one project.

    ``project_id`` is applied to both partitioned tables (and carried into the
    join) so the planner prunes to the single project's partition instead of
    fanning out across all of them. Pass the columns to select (defaults to the
    whole ``LogEvent`` entity); extend the returned ``Select`` with the usual
    ``.where()`` / ``.order_by()`` / ``.limit()`` (e.g. a ``context_id`` or a
    ``data``-field predicate).

    Pass ``owner_key`` (only for single-owner assistant/team contexts -- see
    ``orchestra.db.scope.single_owner_key``) to *also* prune to that owner's
    sub-partition within the Assistants project. Applied as a literal on both
    tables (plus the owner equijoin in the join) for reliable plan-time pruning.

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
        .join(LogEventContext, log_event_context_join(owner_key=owner_key))
        .where(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            owner_scope_clause(LogEvent, owner_key),
            owner_scope_clause(LogEventContext, owner_key),
        )
    )
