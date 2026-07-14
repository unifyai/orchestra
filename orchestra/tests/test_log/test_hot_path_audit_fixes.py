"""Regression coverage for the post-partition hot-path audit fixes."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from orchestra.db.dao.embedding_dao import EmbeddingDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.unique_constraint_dao import UniqueConstraintDAO
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


@pytest.mark.anyio
async def test_flat_group_by_null_bucket_and_groups_only(client: AsyncClient):
    """Flat view-pane grouping uses SQL GROUP BY + null via EXCEPT."""
    project_name = f"flat-group-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)

    for entries in (
        {"color": "red", "n": 1},
        {"color": "red", "n": 2},
        {"color": "blue", "n": 3},
        {"n": 4},  # missing color → null bucket
    ):
        create = await client.post(
            "/v0/logs",
            json={"project_name": project_name, "entries": entries},
            headers=HEADERS,
        )
        assert create.status_code == 200, create.json()

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/color"],
            "nested_groups": False,
            "groups_only": True,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body.get("logs") == []
    groups = body["groups"]["entries/color"]
    assert groups["count"] == 4
    assert groups["group_count"] == 3
    assert set(groups["red"]) | set(groups["blue"]) | set(groups["null"])
    assert len(groups["red"]) == 2
    assert len(groups["blue"]) == 1
    assert len(groups["null"]) == 1


@pytest.mark.anyio
async def test_logs_groups_uses_distinct_not_full_scan(client: AsyncClient):
    project_name = f"groups-distinct-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)

    for i in range(5):
        create = await client.post(
            "/v0/logs",
            json={
                "project_name": project_name,
                "entries": {"tag": "alpha" if i < 3 else "beta", "i": i},
            },
            headers=HEADERS,
        )
        assert create.status_code == 200, create.json()

    resp = await client.get(
        f"/v0/logs/groups?project_name={project_name}&key=tag",
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    values = set(resp.json().values())
    assert values == {"alpha", "beta"}


@pytest.mark.anyio
async def test_embedding_dao_requires_project_id_with_ids(dbsession):
    dao = EmbeddingDAO(dbsession)
    with pytest.raises(ValueError, match="project_id is required"):
        dao.cancel_queue(log_event_ids=[1], project_id=None)
    with pytest.raises(ValueError, match="project_id is required"):
        dao.soft_delete(log_event_ids=[1], project_id=None)


@pytest.mark.anyio
async def test_unique_field_conflict_lookup_is_batched(dbsession):
    project_id = dbsession.execute(
        text(
            "INSERT INTO project (name, user_id) VALUES (:n, :u) RETURNING id",
        ),
        {"n": f"luc-batch-{uuid.uuid4().hex[:8]}", "u": "audit-user"},
    ).scalar_one()
    context_id = dbsession.execute(
        text(
            "INSERT INTO context (project_id, name, allow_duplicates) "
            "VALUES (:pid, 'c', true) RETURNING id",
        ),
        {"pid": project_id},
    ).scalar_one()
    existing_id = dbsession.execute(
        text(
            "INSERT INTO log_event (project_id, owner_key, data) "
            "VALUES (:pid, 'sys', '{\"email\": \"a@example.com\"}'::jsonb) "
            "RETURNING id",
        ),
        {"pid": project_id},
    ).scalar_one()
    dao = UniqueConstraintDAO(dbsession)
    value_hash = dao.hash_value("a@example.com")
    dbsession.execute(
        text(
            "INSERT INTO log_unique_constraint "
            "(context_id, project_id, field_name, value_hash, log_event_id) "
            "VALUES (:cid, :pid, 'email', :vh, :lid)",
        ),
        {
            "cid": context_id,
            "pid": project_id,
            "vh": value_hash,
            "lid": existing_id,
        },
    )
    candidate_id = dbsession.execute(
        text(
            "INSERT INTO log_event (project_id, owner_key, data) "
            "VALUES (:pid, 'sys', '{\"email\": \"a@example.com\"}'::jsonb) "
            "RETURNING id",
        ),
        {"pid": project_id},
    ).scalar_one()
    dbsession.commit()

    conflicts = dao.find_unique_field_conflict_log_ids(
        context_id=context_id,
        project_id=project_id,
        log_entries=[(candidate_id, {"email": "a@example.com"})],
        unique_fields={"email"},
    )
    assert candidate_id in conflicts
    assert conflicts[candidate_id][0] == "email"


@pytest.mark.anyio
async def test_log_event_delete_batch_with_commit_false(dbsession):
    project_id = dbsession.execute(
        text(
            "INSERT INTO project (name, user_id) VALUES (:n, :u) RETURNING id",
        ),
        {"n": f"del-batch-{uuid.uuid4().hex[:8]}", "u": "audit-user"},
    ).scalar_one()
    ids = []
    for i in range(3):
        ids.append(
            dbsession.execute(
                text(
                    "INSERT INTO log_event (project_id, owner_key, data) "
                    "VALUES (:pid, 'sys', CAST(:d AS jsonb)) RETURNING id",
                ),
                {"pid": project_id, "d": f'{{"n": {i}}}'},
            ).scalar_one(),
        )
    dbsession.commit()

    LogEventDAO(dbsession).delete(ids, commit=False, project_id=project_id)
    dbsession.commit()

    remaining = dbsession.execute(
        text(
            "SELECT count(*) FROM log_event "
            "WHERE project_id = :pid AND id = ANY(:ids)",
        ),
        {"pid": project_id, "ids": ids},
    ).scalar_one()
    assert remaining == 0
