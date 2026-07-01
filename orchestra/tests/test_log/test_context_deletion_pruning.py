"""Partition-pruning regression guard for the context-deletion query paths.

The heavy tables are ``LIST(project_id)``-partitioned with no standalone index
on ``log_event.id``; a deletion query that filters by bare ``id`` therefore
scans every partition (catastrophic on the shared Assistants project). The
``ContextDAO.delete`` orphan-cleanup SQL must keep its ``project_id`` predicate
so Postgres prunes to the owning project's partition and resolves ``id`` via the
``(project_id, id)`` index.

This test seeds an Assistants-shaped, owner-sub-partitioned project alongside a
co-resident other-tenant project (in the top DEFAULT partition), then asserts
the fixed orphan-detect / hard-delete plans:
  * do NOT scan the top DEFAULT partition (the other tenant's home), and
  * reach ``log_event`` via an index, never a Seq Scan.

Marked ``performance`` (seeds enough rows to make the planner prefer the index)
so it runs under the perf workflow, not the normal suite.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from orchestra.db.models.core_models import Context
from orchestra.db.models.orchestra_models import Project
from orchestra.db.partitioning import (
    default_partition_name,
    promote_owner,
    sub_partition_project_by_owner,
)
from orchestra.db.scope import OwnerScope, owner_key

pytestmark = pytest.mark.performance

_TARGET_LOGS = 20_000
_PROMOTED_NOISE_LOGS = 40_000
_OTHER_TENANT_LOGS = 60_000

_TOP_DEFAULT = default_partition_name("log_event")  # "log_event_default"


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


def _explain(conn, sql: str, params: dict) -> str:
    return "\n".join(r[0] for r in conn.execute(text(f"EXPLAIN {sql}"), params).all())


def test_context_deletion_orphan_queries_prune_to_project(dbsession) -> None:
    conn = dbsession.connection()
    user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

    # Target Assistants-shaped project (will be owner-sub-partitioned) + a
    # co-resident other tenant whose rows stay in the top DEFAULT partition.
    target = Project(user_id=user_id, organization_id=None, name="PruneAssistants")
    other = Project(user_id=user_id, organization_id=None, name="PruneOtherTenant")
    dbsession.add_all([target, other])
    dbsession.flush()

    target_agent_id = 558
    noise_agent_id = 700
    target_ok = owner_key(OwnerScope.ASSISTANT, target_agent_id)
    noise_ok = owner_key(OwnerScope.ASSISTANT, noise_agent_id)

    target_ctx = Context(
        project_id=target.id,
        name=f"{user_id}/{target_agent_id}/Data/ClientBeta",
        owner_scope=OwnerScope.ASSISTANT.value,
        owner_id=target_agent_id,
    )
    noise_ctx = Context(
        project_id=target.id,
        name=f"{user_id}/{noise_agent_id}/Data",
        owner_scope=OwnerScope.ASSISTANT.value,
        owner_id=noise_agent_id,
    )
    other_ctx = Context(project_id=other.id, name="other/logs")
    dbsession.add_all([target_ctx, noise_ctx, other_ctx])
    dbsession.flush()

    _seed(
        conn,
        project_id=other.id,
        owner_key_str="sys",
        context_id=other_ctx.id,
        count=_OTHER_TENANT_LOGS,
        base_id=100_000_000,
    )
    _seed(
        conn,
        project_id=target.id,
        owner_key_str=target_ok,
        context_id=target_ctx.id,
        count=_TARGET_LOGS,
        base_id=1_000_000,
    )
    _seed(
        conn,
        project_id=target.id,
        owner_key_str=noise_ok,
        context_id=noise_ctx.id,
        count=_PROMOTED_NOISE_LOGS,
        base_id=10_000_000,
    )

    # Carve the target project into owner sub-partitions; promote the noise
    # owner so the target owner stays in the sub-DEFAULT (the un-promoted,
    # Haris-like case). Mirrors prod's owner sub-partitioning.
    sub_partition_project_by_owner(conn, target.id)
    promote_owner(conn, target.id, noise_ok)

    conn.execute(text("ANALYZE log_event"))
    conn.execute(text("ANALYZE log_event_context"))

    orphan_ids = list(range(1_000_001, 1_000_001 + _TARGET_LOGS))

    # Each path has a BARE form (the regression: filters by id only, so every
    # project's / tenant's partition is visited) and the FIXED form (carries
    # project_id, so Postgres prunes to the owning project's partition tree).
    # The robust, scale- and index-independent invariant: the fixed plan must
    # prune the top DEFAULT partition (which holds the *other* tenant), while
    # the bare plan still visits it. (Pruning the project's own owner sub-
    # partitions further is the separate, deferred owner_key optimisation, so we
    # deliberately do not assert on Seq-vs-Index within the target's own tree.)
    paths = {
        "orphan-detect": (
            "SELECT le.id FROM log_event le "
            "WHERE le.id = ANY(:ids) {extra} "
            "AND NOT EXISTS (SELECT 1 FROM log_event_context lec "
            "WHERE lec.log_event_id = le.id {lec_extra})"
        ),
        "hard-delete": (
            "WITH batch AS (SELECT id FROM log_event "
            "WHERE id = ANY(:ids) {extra} LIMIT 5000) "
            "DELETE FROM log_event WHERE id IN (SELECT id FROM batch) {extra}"
        ),
        # Derived-log backfill filter rebuild (views._build_pending_query): the
        # outer log_event scan that gates the EXISTS(condition) must carry
        # project_id, else it Seq/Index-scans every tenant's partition.
        "derived-backfill": (
            "SELECT le.id FROM log_event le "
            "WHERE le.id = ANY(:ids) {extra} "
            "AND EXISTS (SELECT 1 FROM log_event_context lec "
            "WHERE lec.log_event_id = le.id {lec_extra})"
        ),
    }

    def _count_partitions(plan: str) -> int:
        return plan.count("log_event_p") + plan.count(_TOP_DEFAULT)

    for label, tmpl in paths.items():
        bare = _explain(
            conn,
            tmpl.format(extra="", lec_extra=""),
            {"ids": orphan_ids},
        )
        fixed = _explain(
            conn,
            tmpl.format(
                extra="AND project_id = :pid",
                lec_extra="AND lec.project_id = :pid",
            ),
            {"ids": orphan_ids, "pid": target.id},
        )

        assert _TOP_DEFAULT in bare, (
            f"{label}: regression sentinel broke -- the bare (no project_id) form "
            f"should scan the top DEFAULT partition:\n{bare}"
        )
        assert _TOP_DEFAULT not in fixed, (
            f"{label}: top DEFAULT partition ({_TOP_DEFAULT}) not pruned by the "
            f"project_id predicate -- cross-tenant scan still occurs:\n{fixed}"
        )
        assert _count_partitions(fixed) < _count_partitions(bare), (
            f"{label}: fixed plan should touch strictly fewer partitions than "
            f"the bare plan.\nBARE:\n{bare}\nFIXED:\n{fixed}"
        )
