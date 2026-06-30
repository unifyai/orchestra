"""Owner-key sub-partition pruning guard for the single-owner read paths.

Within the shared Assistants project the heavy tables are sub-partitioned
``LIST (owner_key)``. A read scoped to one assistant/team context should carry an
``owner_key`` predicate so the planner prunes to that owner's sub-partition
instead of ``Append``-ing across every owner. This test promotes a reserved
owner *sentinel* sub-partition and asserts the optimized read patterns prune it
out (while the un-scoped form still scans it -- the negative control proves the
guard is meaningful).

Adding a new single-owner read pattern? Add it to ``_owner_scoped_scenarios`` so
it is asserted to prune -- that is the "don't miss future optimization
opportunities" registry for the owner_key optimization.

Pruning a ``LIST(owner_key)`` partition by a literal ``owner_key`` happens at
plan time, so this needs no perf-scale seeding and runs in the normal suite.
"""

from __future__ import annotations

import os

from sqlalchemy import select, text

from orchestra.db.log_queries import (
    log_event_context_join,
    owner_scope_clause,
    project_scoped_log_events,
)
from orchestra.db.models.core_models import Context
from orchestra.db.models.orchestra_models import LogEvent, LogEventContext, Project
from orchestra.db.scope import OwnerScope, owner_key
from orchestra.tests.partition_prune_guard import (
    _SENTINEL_OWNER_TOKEN,
    _explain_text,
    assert_prunes_to_owner,
    setup_owner_partitions,
)

_TARGET_AGENT_ID = 558
_NOISE_AGENT_ID = 700


def _seed(conn, *, project_id, owner_key_str, context_id, count, base_id):
    conn.execute(
        text(
            "INSERT INTO log_event (project_id, id, owner_key, data) "
            "SELECT :pid, :base + g, :ok, '{}'::jsonb "
            "FROM generate_series(1, :n) AS g",
        ),
        {"pid": project_id, "base": base_id, "ok": owner_key_str, "n": count},
    )
    conn.execute(
        text(
            "INSERT INTO log_event_context "
            "(project_id, log_event_id, context_id, owner_key) "
            "SELECT :pid, :base + g, :cid, :ok "
            "FROM generate_series(1, :n) AS g",
        ),
        {
            "pid": project_id,
            "base": base_id,
            "cid": context_id,
            "ok": owner_key_str,
            "n": count,
        },
    )


def _owner_scoped_scenarios(*, project_id, context_id, ok):
    """Registry of single-owner read patterns that must prune to one owner.

    Each value is a SQLAlchemy Select that carries the ``owner_key`` predicate the
    real read paths now thread in (mirrors project_scoped_log_events callers and
    the /logs context-scoped scan)."""
    return {
        # Shared helper used by coordinator_service / assistant_bootstrap /
        # task_machine_state_service single-context reads.
        "project_scoped_log_events(owner_key=)": (
            project_scoped_log_events(project_id, LogEvent.id, owner_key=ok).where(
                LogEventContext.context_id == context_id,
            )
        ),
        # The /logs context-scoped scan (logging_utils._get_logs_query).
        "logs context scan": (
            select(LogEvent.id)
            .join(LogEventContext, log_event_context_join(owner_key=ok))
            .where(
                LogEvent.project_id == project_id,
                LogEventContext.project_id == project_id,
                LogEventContext.context_id == context_id,
                owner_scope_clause(LogEvent, ok),
                owner_scope_clause(LogEventContext, ok),
            )
        ),
    }


def test_single_owner_reads_prune_to_owner_subpartition(dbsession) -> None:
    conn = dbsession.connection()
    user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

    target = Project(user_id=user_id, organization_id=None, name="OwnerPruneAssistants")
    dbsession.add(target)
    dbsession.flush()

    target_ok = owner_key(OwnerScope.ASSISTANT, _TARGET_AGENT_ID)
    noise_ok = owner_key(OwnerScope.ASSISTANT, _NOISE_AGENT_ID)

    target_ctx = Context(
        project_id=target.id,
        name=f"{user_id}/{_TARGET_AGENT_ID}/Coordinator/State",
        owner_scope=OwnerScope.ASSISTANT.value,
        owner_id=_TARGET_AGENT_ID,
    )
    noise_ctx = Context(
        project_id=target.id,
        name=f"{user_id}/{_NOISE_AGENT_ID}/Coordinator/State",
        owner_scope=OwnerScope.ASSISTANT.value,
        owner_id=_NOISE_AGENT_ID,
    )
    dbsession.add_all([target_ctx, noise_ctx])
    dbsession.flush()

    _seed(
        conn,
        project_id=target.id,
        owner_key_str=target_ok,
        context_id=target_ctx.id,
        count=5,
        base_id=1_000_000,
    )
    _seed(
        conn,
        project_id=target.id,
        owner_key_str=noise_ok,
        context_id=noise_ctx.id,
        count=5,
        base_id=2_000_000,
    )

    # Owner-sub-partition the project; promote both real owners + the sentinel.
    setup_owner_partitions(conn, target.id, [target_ok, noise_ok])

    scenarios = _owner_scoped_scenarios(
        project_id=target.id,
        context_id=target_ctx.id,
        ok=target_ok,
    )
    for label, query in scenarios.items():
        try:
            assert_prunes_to_owner(dbsession, query, owner_key=target_ok)
        except AssertionError as exc:  # add the scenario label for triage
            raise AssertionError(f"[{label}] {exc}") from exc

    # Negative control: the same scan WITHOUT an owner_key predicate must still
    # scan the owner sentinel sub-partition -- proving the assertions above are
    # meaningful (the sentinel is reachable when not pruned).
    unscoped = (
        select(LogEvent.id)
        .join(LogEventContext, log_event_context_join())
        .where(
            LogEvent.project_id == target.id,
            LogEventContext.project_id == target.id,
            LogEventContext.context_id == target_ctx.id,
        )
    )
    plan = _explain_text(dbsession, unscoped)
    assert _SENTINEL_OWNER_TOKEN in plan, (
        "negative control broke: an owner-agnostic scan should Append across the "
        f"owner sentinel sub-partition:\n{plan}"
    )
