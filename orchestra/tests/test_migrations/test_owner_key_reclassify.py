"""Tests for the corrective owner_key backfill (reclassify_heavy_owner_keys).

Reproduces the production bug: heavy rows stuck at ``owner_key='sys'`` whose
owning context actually identifies an assistant/team. Asserts the corrective
pass flips those to the real owner (and resyncs associations + embeddings) while
leaving genuinely system-owned rows and already-correct rows untouched.
"""

from __future__ import annotations

import os

from sqlalchemy import text

from orchestra.db.models.core_models import (
    Context,
    Embedding,
    LogEvent,
    LogEventContext,
)
from orchestra.db.models.orchestra_models import Project
from orchestra.db.scope import reclassify_heavy_owner_keys

AGENT_ID = 808080


def _owner_key(session, table, **where) -> str:
    cols = " AND ".join(f"{k} = :{k}" for k in where)
    return session.execute(
        text(f"SELECT owner_key FROM {table} WHERE {cols}"),
        where,
    ).scalar()


def test_reclassify_fixes_sys_rows_with_real_owner(dbsession) -> None:
    uid = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
    project = Project(user_id=uid, organization_id=None, name="ReclassifyTest")
    dbsession.add(project)
    dbsession.flush()

    asst_ctx = Context(
        project_id=project.id,
        name=f"{uid}/{AGENT_ID}/Events",
        owner_scope="assistant",
        owner_id=AGENT_ID,
    )
    sys_ctx = Context(
        project_id=project.id,
        name="Builtins/Shared",
        owner_scope="system",
        owner_id=None,
    )
    dbsession.add_all([asst_ctx, sys_ctx])
    dbsession.flush()

    def mk_log(ctx, owner_key):
        le = LogEvent(project_id=project.id, owner_key=owner_key, data={})
        dbsession.add(le)
        dbsession.flush()
        dbsession.add(
            LogEventContext(
                project_id=project.id,
                log_event_id=le.id,
                context_id=ctx.id,
                owner_key=owner_key,
            ),
        )
        dbsession.add(
            Embedding(
                project_id=project.id,
                ref_id=le.id,
                owner_key=owner_key,
                model="m",
                key="k",
                vector=[0.1, 0.2],
            ),
        )
        dbsession.flush()
        return le.id

    # (a) assistant log wrongly stuck at 'sys' -> must become a{AGENT_ID}
    mislabelled = mk_log(asst_ctx, "sys")
    # (b) genuinely system-owned log (system context) -> stays 'sys'
    genuine_sys = mk_log(sys_ctx, "sys")
    # (c) assistant log already correctly tagged -> unchanged
    already_ok = mk_log(asst_ctx, f"a{AGENT_ID}")

    reclassify_heavy_owner_keys(dbsession.connection())
    dbsession.expire_all()

    expected = f"a{AGENT_ID}"
    # (a) fixed across all three heavy tables
    assert (
        _owner_key(dbsession, "log_event", project_id=project.id, id=mislabelled)
        == expected
    )
    assert (
        _owner_key(
            dbsession,
            "log_event_context",
            project_id=project.id,
            log_event_id=mislabelled,
        )
        == expected
    )
    assert (
        _owner_key(dbsession, "embedding", project_id=project.id, ref_id=mislabelled)
        == expected
    )
    # (b) genuine system rows untouched
    assert (
        _owner_key(dbsession, "log_event", project_id=project.id, id=genuine_sys)
        == "sys"
    )
    assert (
        _owner_key(
            dbsession,
            "log_event_context",
            project_id=project.id,
            log_event_id=genuine_sys,
        )
        == "sys"
    )
    # (c) already-correct rows unchanged
    assert (
        _owner_key(dbsession, "log_event", project_id=project.id, id=already_ok)
        == expected
    )
