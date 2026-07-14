"""Regression coverage for the post-partition hot-path audit fixes."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.settings import UniqueValidationMode, settings

from . import HEADERS, _create_project


@pytest.mark.anyio
async def test_get_logs_defaults_to_bounded_page(client: AsyncClient):
    project_name = f"limit-default-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)

    create = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": [{"n": i} for i in range(15)],
        },
        headers=HEADERS,
    )
    assert create.status_code == 200, create.json()

    resp = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["count"] == 15
    assert len(resp.json()["logs"]) == 15


@pytest.mark.anyio
async def test_rename_field_is_set_based(client: AsyncClient):
    project_name = f"rename-set-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)

    create = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "old_field_name": "v",
                "other": 1,
                "explicit_types": {
                    "old_field_name": {"type": "str", "mutable": True},
                    "other": {"type": "int", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert create.status_code == 200, create.json()

    rename = await client.patch(
        "/v0/logs/rename_field",
        json={
            "project_name": project_name,
            "old_field_name": "old_field_name",
            "new_field_name": "new_field_name",
        },
        headers=HEADERS,
    )
    assert rename.status_code == 200, rename.json()

    logs = await client.get(
        f"/v0/logs?project_name={project_name}&limit=100",
        headers=HEADERS,
    )
    assert logs.status_code == 200
    for log in logs.json()["logs"]:
        assert "new_field_name" in log["entries"]
        assert "old_field_name" not in log["entries"]


@pytest.mark.anyio
async def test_add_logs_batch_duplicate_rejection(client: AsyncClient):
    project_name = f"add-logs-dup-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)

    ctx = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": "nodup", "allow_duplicates": False},
        headers=HEADERS,
    )
    assert ctx.status_code == 200, ctx.json()

    create = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "context": "nodup",
            "entries": {"k": "same"},
        },
        headers=HEADERS,
    )
    assert create.status_code == 200, create.json()

    twin = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {"k": "same"},
        },
        headers=HEADERS,
    )
    assert twin.status_code == 200, twin.json()
    twin_id = twin.json()["log_event_ids"][0]

    add_twin = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={
            "context_name": "nodup",
            "log_ids": [twin_id],
            "copy": False,
        },
        headers=HEADERS,
    )
    assert add_twin.status_code == 400, add_twin.json()
    assert "uplicate" in str(add_twin.json())


@pytest.mark.anyio
async def test_reproject_logs_moves_unique_constraint_project_id(dbsession):
    suffix = uuid.uuid4().hex[:8]
    project_a = dbsession.execute(
        text(
            "INSERT INTO project (name, user_id) " "VALUES (:n, :u) RETURNING id",
        ),
        {"n": f"reproject-a-{suffix}", "u": "audit-user"},
    ).scalar_one()
    project_b = dbsession.execute(
        text(
            "INSERT INTO project (name, user_id) " "VALUES (:n, :u) RETURNING id",
        ),
        {"n": f"reproject-b-{suffix}", "u": "audit-user"},
    ).scalar_one()
    context_id = dbsession.execute(
        text(
            "INSERT INTO context (project_id, name, allow_duplicates) "
            "VALUES (:pid, 'c', true) RETURNING id",
        ),
        {"pid": project_a},
    ).scalar_one()
    log_id = dbsession.execute(
        text(
            "INSERT INTO log_event (project_id, owner_key, data) "
            "VALUES (:pid, 'sys', '{\"x\": 1}'::jsonb) RETURNING id",
        ),
        {"pid": project_a},
    ).scalar_one()
    dbsession.execute(
        text(
            "INSERT INTO log_event_context "
            "(project_id, log_event_id, context_id, owner_key) "
            "VALUES (:pid, :lid, :cid, 'sys')",
        ),
        {"pid": project_a, "lid": log_id, "cid": context_id},
    )
    dbsession.execute(
        text(
            "INSERT INTO log_unique_constraint "
            "(context_id, project_id, log_event_id, field_name, value_hash) "
            "VALUES (:cid, :pid, :lid, 'x', 'hash')",
        ),
        {"cid": context_id, "pid": project_a, "lid": log_id},
    )
    dbsession.commit()

    LogEventDAO(dbsession).reproject_logs([log_id], project_b)
    dbsession.commit()

    luc_pid = dbsession.execute(
        text(
            "SELECT project_id FROM log_unique_constraint WHERE log_event_id = :lid",
        ),
        {"lid": log_id},
    ).scalar_one()
    assert luc_pid == project_b


def test_jsonb_scan_refused_outside_test(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_UNIQUE_VALIDATION_MODE", "jsonb_scan")
    monkeypatch.delenv("ORCHESTRA_ALLOW_JSONB_SCAN_UNIQUE_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ORCHESTRA_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="jsonb_scan"):
        settings.assert_unique_validation_mode_safe()
    monkeypatch.setenv("ORCHESTRA_UNIQUE_VALIDATION_MODE", "lookup_table")
    assert settings.unique_validation_mode == UniqueValidationMode.LOOKUP_TABLE


@pytest.mark.anyio
async def test_delete_fields_set_based(client: AsyncClient):
    project_name = f"delete-fields-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)

    create = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {
                "keep": 1,
                "drop_me": 9,
                "explicit_types": {
                    "keep": {"type": "int", "mutable": True},
                    "drop_me": {"type": "int", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert create.status_code == 200, create.json()

    delete = await client.request(
        "DELETE",
        "/v0/logs/fields",
        json={
            "project_name": project_name,
            "fields": ["drop_me"],
        },
        headers=HEADERS,
    )
    assert delete.status_code == 200, delete.json()
    assert "drop_me" in delete.json()["deleted_fields"]

    logs = await client.get(
        f"/v0/logs?project_name={project_name}&limit=20",
        headers=HEADERS,
    )
    assert logs.status_code == 200
    for log in logs.json()["logs"]:
        assert "drop_me" not in log["entries"]
        assert log["entries"]["keep"] == 1


@pytest.mark.anyio
async def test_bulk_merge_data_scopes_project(dbsession):
    project_id = dbsession.execute(
        text(
            "INSERT INTO project (name, user_id) " "VALUES (:n, :u) RETURNING id",
        ),
        {"n": f"bulk-merge-{uuid.uuid4().hex[:8]}", "u": "audit-user"},
    ).scalar_one()
    log_id = dbsession.execute(
        text(
            "INSERT INTO log_event (project_id, owner_key, data) "
            "VALUES (:pid, 'sys', '{\"a\": 1}'::jsonb) RETURNING id",
        ),
        {"pid": project_id},
    ).scalar_one()
    dbsession.commit()

    LogEventDAO(dbsession).bulk_merge_data(
        [{"log_event_id": log_id, "key": "b", "value": 2}],
        project_id=project_id,
    )
    dbsession.commit()

    data = dbsession.execute(
        text("SELECT data FROM log_event WHERE id = :lid"),
        {"lid": log_id},
    ).scalar_one()
    assert data["a"] == 1
    assert data["b"] == 2
