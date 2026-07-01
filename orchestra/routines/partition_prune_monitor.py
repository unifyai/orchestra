"""Detect unpruned queries against the ``LIST(project_id)``-partitioned tables.

``log_event``, ``log_event_context``, ``embedding`` and ``embedding_queue`` are
partitioned by ``project_id``. A query that does not constrain the partition key
fans out across every tenant's partition -- the root cause of the June 2026
production slowdown. The test-time ``partition_prune_guard`` catches this, but
only on code paths that tests actually execute; this routine is the production
safety net that covers *every* path (cron jobs, admin tools, rarely-hit
endpoints, and anything new) by scanning ``pg_stat_statements`` for queries that
touch a partitioned table without a ``project_id`` predicate.

It is read-only (reads ``pg_stat_statements``), so it is safe to run against
prod on a schedule; it reports offenders sorted by total execution time so a
newly-introduced heavy unpruned query stands out.

Scheduling Options:
1. GitHub Actions: .github/workflows/partition-prune-monitor.yml
   - Calls POST /v0/admin/partition-prune-monitor
2. Cloud Scheduler: call the admin endpoint on a schedule.
3. Manual: call the admin endpoint for a one-off scan.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)

# Partitioned tables whose scans must be pruned by project_id.
_PARTITIONED_TABLES = ("log_event_context", "log_event", "embedding_queue", "embedding")

# A qualifier ".project_id" bound to one of these aliases does NOT prune a
# partitioned table (``context`` is a non-partitioned table joined for its name).
_NON_PARTITION_ALIASES = {"context", "ctx", "c"}

_QUALIFIED_PROJECT_ID = re.compile(r"(\w+)\.project_id", re.IGNORECASE)
# A bare (unqualified) project_id predicate, e.g. single-table ``WHERE project_id = ...``.
_BARE_PROJECT_ID = re.compile(r"\bproject_id\s*(=|in\b|=\s*any)", re.IGNORECASE)


def _query_prunes(query: str) -> bool:
    """Heuristic: does the query carry a project_id predicate that can prune a
    partitioned table? Conservative on the safe side -- a project_id qualifier on
    any partitioned-table alias (or a bare single-table predicate) counts as
    pruned; only ``context.project_id`` alone does not."""
    quals = [a.lower() for a in _QUALIFIED_PROJECT_ID.findall(query)]
    if any(a not in _NON_PARTITION_ALIASES for a in quals):
        return True
    # No qualified project_id on a partitioned alias: accept a bare predicate
    # (single-table scans have no alias) as long as it is not the context one.
    if not quals and _BARE_PROJECT_ID.search(query):
        return True
    return False


def run_partition_prune_monitor(
    session: Optional[Session] = None,
    *,
    limit: int = 1000,
) -> dict[str, Any]:
    """Scan ``pg_stat_statements`` for unpruned partitioned-table queries.

    Returns a summary dict with the list of offenders (queryid, calls, timings,
    tables, truncated SQL). Read-only. If ``pg_stat_statements`` is unavailable
    (e.g. a local/test database without the extension) it returns an
    ``available: False`` summary rather than raising.
    """
    if session is not None:
        return _scan(session, limit=limit)
    session_local = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with session_local() as owned_session:
        return _scan(owned_session, limit=limit)


def _scan(session: Session, *, limit: int) -> dict[str, Any]:
    table_pattern = r"\y(" + "|".join(_PARTITIONED_TABLES) + r")\y"
    try:
        rows = session.execute(
            text(
                """
                SELECT queryid,
                       calls,
                       total_exec_time,
                       mean_exec_time,
                       query
                FROM pg_stat_statements
                WHERE query ~* :table_pattern
                  AND query ~* '^\\s*(select|with|update|delete)'
                ORDER BY total_exec_time DESC
                LIMIT :limit
                """,
            ),
            {"table_pattern": table_pattern, "limit": limit},
        ).fetchall()
    except Exception as exc:  # pg_stat_statements not installed / no permission
        logger.warning(
            "partition-prune monitor: pg_stat_statements unavailable: %s",
            exc,
        )
        session.rollback()
        return {"available": False, "reason": str(exc), "offenders": []}

    offenders: list[dict[str, Any]] = []
    for queryid, calls, total_ms, mean_ms, query in rows:
        normalized = " ".join(query.split())
        if _query_prunes(normalized):
            continue
        tables = [t for t in _PARTITIONED_TABLES if re.search(rf"\b{t}\b", normalized)]
        offenders.append(
            {
                "queryid": queryid,
                "calls": calls,
                "total_exec_ms": round(total_ms or 0.0, 1),
                "mean_exec_ms": round(mean_ms or 0.0, 2),
                "tables": tables,
                "query": normalized[:300],
            },
        )

    summary = {
        "available": True,
        "scanned": len(rows),
        "unpruned_count": len(offenders),
        "offenders": offenders,
    }
    if offenders:
        top = offenders[0]
        logger.warning(
            "partition-prune monitor: %d unpruned partitioned-table queries "
            "(top: queryid=%s calls=%s total=%.0fms tables=%s). These fan out "
            "across every tenant's partition; add a project_id predicate "
            "(orchestra/db/log_queries.py) or allowlist if genuinely global.",
            len(offenders),
            top["queryid"],
            top["calls"],
            top["total_exec_ms"],
            top["tables"],
        )
    else:
        logger.info("partition-prune monitor: no unpruned partitioned-table queries")
    return summary
