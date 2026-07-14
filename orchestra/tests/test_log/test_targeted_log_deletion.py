"""Targeted log deletion must stay O(deleted rows), not O(context size).

Also covers the uniqueness-seam cleanup: deleting unique-keyed logs must clear
scoped ``log_unique_constraint`` rows (with ``project_id``) so recreate works.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from orchestra.db.models.core_models import LogUniqueConstraint

from . import HEADERS, _create_project, _delete_logs, _get_log


@pytest.mark.anyio
async def test_targeted_delete_does_not_scan_full_context_membership(
    client: AsyncClient,
    dbsession,
):
    """Id-list deletes must not SELECT every log_event_id in the context."""
    project_name = f"targeted-delete-scan-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)
    context = "large"

    ctx_resp = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context},
        headers=HEADERS,
    )
    assert ctx_resp.status_code == 200, ctx_resp.json()

    create = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "context": context,
            "entries": [{"tag": f"row-{i}"} for i in range(40)],
        },
        headers=HEADERS,
    )
    assert create.status_code == 200, create.json()
    log_ids = create.json()["log_event_ids"]
    assert len(log_ids) == 40

    project_id = dbsession.execute(
        text("SELECT id FROM project WHERE name = :n"),
        {"n": project_name},
    ).scalar()
    context_id = dbsession.execute(
        text(
            "SELECT id FROM context WHERE project_id = :pid AND name = :name",
        ),
        {"pid": project_id, "name": context},
    ).scalar()

    # Inflate context membership far beyond the API-created set so a full
    # context scan would be obviously wrong if it reappeared.
    dbsession.execute(
        text(
            "INSERT INTO log_event (project_id, id, owner_key, data) "
            "SELECT :pid, :base + g, 'sys', '{}'::jsonb "
            "FROM generate_series(1, 500) AS g",
        ),
        {"pid": project_id, "base": 90_000_000},
    )
    dbsession.execute(
        text(
            "INSERT INTO log_event_context "
            "(project_id, log_event_id, context_id, owner_key) "
            "SELECT :pid, :base + g, :cid, 'sys' "
            "FROM generate_series(1, 500) AS g",
        ),
        {"pid": project_id, "base": 90_000_000, "cid": context_id},
    )
    dbsession.commit()

    full_context_scans: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.lower().split())
        if "from log_event_context" not in sql or "select" not in sql:
            return
        # Full-context membership load: filters by context_id (+ project_id)
        # but does not constrain log_event_id.
        if "context_id" in sql and "log_event_id" not in sql.split("where", 1)[-1]:
            full_context_scans.append(statement)

    event.listen(Engine, "before_cursor_execute", _capture)
    try:
        to_delete = log_ids[:5]
        response = await _delete_logs(
            client,
            [(to_delete, None)],
            project_name=project_name,
            context=context,
        )
        assert response.status_code == 200, response.json()
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)

    assert full_context_scans == [], full_context_scans

    for log_id in to_delete:
        gone = await client.get(
            f"/v0/logs?project_name={project_name}&context={context}"
            f"&from_ids={log_id}",
            headers=HEADERS,
        )
        assert gone.status_code == 200
        assert gone.json()["count"] == 0

    for log_id in log_ids[5:10]:
        still = await client.get(
            f"/v0/logs?project_name={project_name}&context={context}"
            f"&from_ids={log_id}",
            headers=HEADERS,
        )
        assert still.status_code == 200
        assert still.json()["count"] == 1


@pytest.mark.anyio
async def test_delete_unique_keyed_logs_clears_scoped_constraints(
    client: AsyncClient,
    dbsession,
):
    """Delete must remove project-scoped uniqueness rows so recreate succeeds."""
    project_name = f"unique-delete-seam-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)
    context = "prospects"

    ctx_resp = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context, "unique_keys": {"github_login": "str"}},
        headers=HEADERS,
    )
    assert ctx_resp.status_code == 200, ctx_resp.json()

    create = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "context": context,
            "entries": [
                {"github_login": "alice", "best_email": "a@example.com"},
                {"github_login": "bob", "best_email": "b@example.com"},
            ],
        },
        headers=HEADERS,
    )
    assert create.status_code == 200, create.json()
    log_ids = create.json()["log_event_ids"]

    project_id = dbsession.execute(
        text("SELECT id FROM project WHERE name = :n"),
        {"n": project_name},
    ).scalar()
    context_id = dbsession.execute(
        text(
            "SELECT id FROM context WHERE project_id = :pid AND name = :name",
        ),
        {"pid": project_id, "name": context},
    ).scalar()

    constraints = (
        dbsession.query(LogUniqueConstraint)
        .filter(LogUniqueConstraint.context_id == context_id)
        .all()
    )
    assert len(constraints) >= 2
    assert all(c.project_id == project_id for c in constraints)

    response = await _delete_logs(
        client,
        [(log_ids, None)],
        project_name=project_name,
        context=context,
    )
    assert response.status_code == 200, response.json()

    dbsession.expire_all()
    left = (
        dbsession.query(LogUniqueConstraint)
        .filter(
            LogUniqueConstraint.context_id == context_id,
            LogUniqueConstraint.project_id == project_id,
        )
        .count()
    )
    assert left == 0

    recreate = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "context": context,
            "entries": {"github_login": "alice", "best_email": "a2@example.com"},
        },
        headers=HEADERS,
    )
    assert recreate.status_code == 200, recreate.json()


@pytest.mark.anyio
async def test_delete_from_one_context_keeps_shared_log_in_other(
    client: AsyncClient,
):
    """Entire-log delete from context A must unlink, not destroy, a shared log."""
    project_name = f"shared-log-unlink-{uuid.uuid4().hex[:8]}"
    await _create_project(client, project_name)

    create = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "entries": {"shared": "value"},
        },
        headers=HEADERS,
    )
    assert create.status_code == 200, create.json()
    log_id = create.json()["log_event_ids"][0]

    other = "other-context"
    ctx_resp = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": other},
        headers=HEADERS,
    )
    assert ctx_resp.status_code == 200, ctx_resp.json()
    add = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": other, "log_ids": [log_id]},
        headers=HEADERS,
    )
    assert add.status_code == 200, add.json()

    # Delete from the secondary context only — log must remain in default.
    response = await _delete_logs(
        client,
        [([log_id], None)],
        project_name=project_name,
        context=other,
    )
    assert response.status_code == 200, response.json()

    still = await _get_log(client, project_name, log_id)
    assert still.status_code == 200
    assert still.json()["count"] == 1
