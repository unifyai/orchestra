"""Backfill typed Knowledge ledger unique/auto-count schema."""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from orchestra.db.knowledge_ledger_schema import backfill_knowledge_ledger_schema
from orchestra.db.models.core_models import (
    Context,
    FieldType,
    LogEvent,
    LogEventContext,
)
from orchestra.db.models.orchestra_models import Project
from orchestra.tests.test_log import HEADERS, _create_log, _create_project, fetch_logs


def _ctx_schema(session, context_id: int) -> tuple[list, list, dict]:
    row = session.execute(
        text(
            """
            SELECT unique_key_names, unique_key_types, auto_counting
            FROM context
            WHERE id = :context_id
            """,
        ),
        {"context_id": context_id},
    ).one()
    return list(row[0] or []), list(row[1] or []), dict(row[2] or {})


def _field_type(session, *, project_id: int, context_id: int):
    return (
        session.query(FieldType)
        .filter_by(
            project_id=project_id,
            context_id=context_id,
            field_name="knowledge_id",
        )
        .one_or_none()
    )


def test_backfill_knowledge_schema_targets_top_level_contexts_only(dbsession) -> None:
    uid = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
    project = Project(user_id=uid, organization_id=None, name="KnowledgeSchemaBackfill")
    dbsession.add(project)
    dbsession.flush()

    bare_root = Context(
        project_id=project.id,
        name="Knowledge",
        unique_key_names=[],
        unique_key_types=[],
        auto_counting={},
    )
    bare_nested = Context(
        project_id=project.id,
        name=f"{uid}/0/Knowledge",
        unique_key_names=[],
        unique_key_types=[],
        auto_counting={},
    )
    already_typed = Context(
        project_id=project.id,
        name=f"{uid}/1/Knowledge",
        unique_key_names=["knowledge_id"],
        unique_key_types=["int"],
        auto_counting={"knowledge_id": None},
    )
    meta = Context(
        project_id=project.id,
        name=f"{uid}/0/Knowledge/Meta",
        unique_key_names=["meta_id"],
        unique_key_types=["int"],
        auto_counting={},
    )
    child = Context(
        project_id=project.id,
        name=f"{uid}/0/Knowledge/Facts",
        unique_key_names=[],
        unique_key_types=[],
        auto_counting={},
    )
    dbsession.add_all([bare_root, bare_nested, already_typed, meta, child])
    dbsession.flush()

    legacy_log = LogEvent(
        project_id=project.id,
        data={"title": "pre-existing", "content": "no id yet"},
    )
    dbsession.add(legacy_log)
    dbsession.flush()
    dbsession.add(
        LogEventContext(
            project_id=project.id,
            log_event_id=legacy_log.id,
            context_id=bare_nested.id,
        ),
    )
    dbsession.flush()

    backfill_knowledge_ledger_schema(dbsession.connection())
    backfill_knowledge_ledger_schema(dbsession.connection())
    dbsession.expire_all()

    for ctx in (bare_root, bare_nested):
        names, types, auto = _ctx_schema(dbsession, ctx.id)
        assert names == ["knowledge_id"]
        assert types == ["int"]
        assert auto == {"knowledge_id": None}
        ft = _field_type(dbsession, project_id=project.id, context_id=ctx.id)
        assert ft is not None
        assert ft.field_type == "int"
        assert ft.mutable is False
        assert ft.unique is True
        assert ft.field_category == "entry"

    typed_names, typed_types, typed_auto = _ctx_schema(dbsession, already_typed.id)
    assert typed_names == ["knowledge_id"]
    assert typed_types == ["int"]
    assert typed_auto == {"knowledge_id": None}
    assert (
        _field_type(dbsession, project_id=project.id, context_id=already_typed.id)
        is not None
    )

    meta_names, meta_types, meta_auto = _ctx_schema(dbsession, meta.id)
    assert meta_names == ["meta_id"]
    assert meta_types == ["int"]
    assert meta_auto == {}
    assert _field_type(dbsession, project_id=project.id, context_id=meta.id) is None

    child_names, child_types, child_auto = _ctx_schema(dbsession, child.id)
    assert child_names == []
    assert child_types == []
    assert child_auto == {}
    assert _field_type(dbsession, project_id=project.id, context_id=child.id) is None

    legacy = (
        dbsession.query(LogEvent)
        .filter_by(project_id=project.id, id=legacy_log.id)
        .one()
    )
    assert "knowledge_id" not in (legacy.data or {})


@pytest.mark.anyio
async def test_backfilled_legacy_knowledge_context_creates_logs_with_knowledge_id(
    client: AsyncClient,
    dbsession,
) -> None:
    project_name = "legacy-knowledge-create-project"
    context_name = "default/0/Knowledge"
    await _create_project(client, project_name)

    bare = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "description": "pre-typed ledger"},
        headers=HEADERS,
    )
    assert bare.status_code == 200, bare.text

    seed = await _create_log(
        client,
        project_name,
        entries={"title": "legacy row", "content": "no auto id"},
        context=context_name,
    )
    assert seed.status_code == 200, seed.text
    assert seed.json()["row_ids"]["names"] == []

    backfill_knowledge_ledger_schema(dbsession.connection())
    dbsession.commit()

    created = await _create_log(
        client,
        project_name,
        entries={"title": "post-backfill", "content": "should auto-count"},
        context=context_name,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["row_ids"]["names"] == ["knowledge_id"]
    assert body["row_ids"]["ids"] == [[0]]
    assert body["auto_counting"]["knowledge_id"] == [0]

    fields = await client.get(
        f"/v0/project/{project_name}/contexts/{context_name}",
        headers=HEADERS,
    )
    assert fields.status_code == 200, fields.text
    assert fields.json()["unique_keys"] == ["knowledge_id"]
    assert fields.json()["auto_counting"] == {"knowledge_id": None}

    legacy_entries = await fetch_logs(
        client,
        project_name,
        context=context_name,
        filter="title == 'legacy row'",
    )
    assert len(legacy_entries) == 1
    assert "knowledge_id" not in legacy_entries[0].get("entries", {})
