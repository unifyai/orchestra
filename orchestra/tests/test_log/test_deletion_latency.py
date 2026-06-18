"""Deletion-latency benchmark for the owner-scoped purge.

Validates the central claim of the partitioning/owner-key work: deleting one
owner (assistant/team) is proportional to *that owner's* rows and uses the
``idx_log_event_project_owner`` index, independent of how much other data shares
the same project. Marked ``performance`` so it only runs under the perf workflow
(``performance_tests.yml``), not the normal suite.

Scale is configurable:
    ORCHESTRA_PERF_DELETE_COUNT   target owner's row count (default 100_000)
    ORCHESTRA_PERF_DELETE_NOISE   co-resident other-owner row count (default 200_000)

Run locally at a higher scale to bracket the real worst case, e.g.:
    ORCHESTRA_PERF_DELETE_COUNT=1000000 ORCHESTRA_PERF_DELETE_NOISE=2000000 \
        .venv/bin/python -m pytest orchestra/tests/test_log/test_deletion_latency.py \
        -o addopts="" -m performance -s
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import text

from orchestra.db.models.core_models import Context
from orchestra.db.models.orchestra_models import Project
from orchestra.db.scope import OwnerScope, owner_key, purge_owner

pytestmark = pytest.mark.performance

_TARGET_COUNT = int(os.getenv("ORCHESTRA_PERF_DELETE_COUNT", "100000"))
_NOISE_COUNT = int(os.getenv("ORCHESTRA_PERF_DELETE_NOISE", "200000"))


def _seed_heavy_rows(
    conn,
    *,
    project_id: int,
    owner_key_str: str,
    context_id: int,
    count: int,
    base_id: int,
) -> None:
    """Bulk-insert ``count`` log_event (+ association) rows for one owner.

    Uses ``generate_series`` so seeding millions of rows is a single statement.
    Ids are explicit and disjoint per owner via ``base_id``.
    """
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


def test_owner_purge_is_indexed_and_proportional(dbsession) -> None:
    conn = dbsession.connection()
    user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

    project = Project(user_id=user_id, organization_id=None, name="PerfAssistants")
    dbsession.add(project)
    dbsession.flush()

    target_agent_id = 700001
    noise_agent_id = 700002
    target_ok = owner_key(OwnerScope.ASSISTANT, target_agent_id)
    noise_ok = owner_key(OwnerScope.ASSISTANT, noise_agent_id)

    target_ctx = Context(
        project_id=project.id,
        name=f"{user_id}/{target_agent_id}/Knowledge",
        owner_scope=OwnerScope.ASSISTANT.value,
        owner_id=target_agent_id,
    )
    noise_ctx = Context(
        project_id=project.id,
        name=f"{user_id}/{noise_agent_id}/Knowledge",
        owner_scope=OwnerScope.ASSISTANT.value,
        owner_id=noise_agent_id,
    )
    dbsession.add_all([target_ctx, noise_ctx])
    dbsession.flush()

    # Co-resident "noise" first, then the target owner, so the project holds a
    # realistic mix and the planner must discriminate by owner_key.
    _seed_heavy_rows(
        conn,
        project_id=project.id,
        owner_key_str=noise_ok,
        context_id=noise_ctx.id,
        count=_NOISE_COUNT,
        base_id=10_000_000,
    )
    _seed_heavy_rows(
        conn,
        project_id=project.id,
        owner_key_str=target_ok,
        context_id=target_ctx.id,
        count=_TARGET_COUNT,
        base_id=20_000_000,
    )
    # Give the planner real statistics for the freshly bulk-loaded partitions.
    conn.execute(text("ANALYZE log_event"))
    conn.execute(text("ANALYZE log_event_context"))

    # Plan check (non-destructive: EXPLAIN without ANALYZE does not execute).
    plan = "\n".join(
        r[0]
        for r in conn.execute(
            text(
                "EXPLAIN DELETE FROM log_event "
                "WHERE project_id = :pid AND owner_key = :ok",
            ),
            {"pid": project.id, "ok": target_ok},
        ).all()
    )
    print(f"\n[delete-latency] target={_TARGET_COUNT} noise={_NOISE_COUNT}")
    print(f"[delete-latency] EXPLAIN DELETE log_event:\n{plan}")
    # The owner-scoped delete must resolve via the (project_id, owner_key) index,
    # not a full scan. The leaf index name differs by provenance (Postgres
    # auto-generates it under meta.create_all in tests, while the migration names
    # it ``*_powner_idx`` in prod), so assert on the access method + index
    # condition rather than the exact name.
    assert (
        "Index Scan" in plan or "Bitmap Index Scan" in plan
    ), f"owner-scoped delete must use an index, got plan:\n{plan}"
    assert "owner_key" in plan, f"index condition must scope by owner_key:\n{plan}"
    assert "Seq Scan" not in plan, f"unexpected Seq Scan in plan:\n{plan}"

    # End-to-end owner purge (heavy tables + owned context tree), timed.
    start = time.monotonic()
    method = purge_owner(
        conn,
        project.id,
        OwnerScope.ASSISTANT.value,
        target_agent_id,
    )
    elapsed = time.monotonic() - start
    print(
        f"[delete-latency] purge_owner method={method} "
        f"rows={_TARGET_COUNT} elapsed={elapsed:.3f}s "
        f"({_TARGET_COUNT / elapsed:,.0f} rows/s)",
    )

    # Target owner fully gone; the co-resident owner is untouched.
    remaining_target = conn.execute(
        text(
            "SELECT count(*) FROM log_event "
            "WHERE project_id = :pid AND owner_key = :ok",
        ),
        {"pid": project.id, "ok": target_ok},
    ).scalar()
    remaining_noise = conn.execute(
        text(
            "SELECT count(*) FROM log_event "
            "WHERE project_id = :pid AND owner_key = :ok",
        ),
        {"pid": project.id, "ok": noise_ok},
    ).scalar()
    assert remaining_target == 0
    assert remaining_noise == _NOISE_COUNT
