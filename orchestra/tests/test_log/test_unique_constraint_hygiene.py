"""Lookup-table hygiene for unique constraints.

``log_unique_constraint`` has no FK to ``log_event`` (dropped for
partitioning), so a constraint row can only be trusted while its holder log
is still attached to the context. These tests pin the two halves of that
contract: delete paths release a log's claims, and the create path reclaims
a lapsed claim instead of reporting a phantom duplicate.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestra.db.dao.unique_constraint_dao import (
    COMPOSITE_KEY_FIELD,
    UniqueConstraintDAO,
)
from orchestra.db.models.core_models import Context, Project

from . import HEADERS, _create_project, _delete_logs


async def _create_keyed_context(
    client: AsyncClient,
    project_name: str,
    context_name: str,
) -> None:
    resp = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name, "unique_keys": {"contact_id": "int"}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()


async def _create_contact_row(
    client: AsyncClient,
    project_name: str,
    context_name: str,
    contact_id: int,
) -> int:
    resp = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "context": context_name,
            "entries": {"contact_id": contact_id, "name": f"row-{contact_id}"},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["log_event_ids"][0]


def _context_id(dbsession: Session, project_name: str, context_name: str) -> int:
    row = (
        dbsession.query(Context.id)
        .join(Project, Project.id == Context.project_id)
        .filter(Project.name == project_name, Context.name == context_name)
        .one()
    )
    return row[0]


def _constraint_holders(dbsession: Session, context_id: int) -> dict[str, int]:
    rows = dbsession.execute(
        text(
            "SELECT value_hash, log_event_id FROM log_unique_constraint "
            "WHERE context_id = :cid AND field_name = :field",
        ),
        {"cid": context_id, "field": COMPOSITE_KEY_FIELD},
    ).fetchall()
    return {row.value_hash: row.log_event_id for row in rows}


def _contact_hash(contact_id: int) -> str:
    return UniqueConstraintDAO.hash_composite(
        {"contact_id": contact_id},
        ["contact_id"],
    )


def _orphan_log(dbsession: Session, log_id: int) -> None:
    """Drop a log and its context association while leaving its constraint
    rows behind — the footprint of a delete path that missed the app-level
    cleanup."""
    dbsession.execute(
        text("DELETE FROM log_event_context WHERE log_event_id = :lid"),
        {"lid": log_id},
    )
    dbsession.execute(
        text("DELETE FROM log_event WHERE id = :lid"),
        {"lid": log_id},
    )
    dbsession.flush()


@pytest.mark.anyio
async def test_create_reclaims_orphaned_composite_key(
    client: AsyncClient,
    dbsession: Session,
):
    """A constraint row whose holder log is gone must not block its key."""
    project_name = "reclaim-orphaned-key"
    await _create_project(client, project_name)
    await _create_keyed_context(client, project_name, "contacts")
    ctx_id = _context_id(dbsession, project_name, "contacts")

    stale_id = await _create_contact_row(client, project_name, "contacts", 0)
    _orphan_log(dbsession, stale_id)
    assert _constraint_holders(dbsession, ctx_id)[_contact_hash(0)] == stale_id

    healed_id = await _create_contact_row(client, project_name, "contacts", 0)
    assert _constraint_holders(dbsession, ctx_id)[_contact_hash(0)] == healed_id

    resp = await client.get(
        f"/v0/logs?project_name={project_name}&context=contacts",
        headers=HEADERS,
    )
    contact_ids = [log["entries"]["contact_id"] for log in resp.json()["logs"]]
    assert contact_ids == [0]


@pytest.mark.anyio
async def test_live_duplicate_still_rejected(client: AsyncClient):
    """Reclaim must not fire while the holder log is attached."""
    project_name = "reclaim-live-duplicate"
    await _create_project(client, project_name)
    await _create_keyed_context(client, project_name, "contacts")
    await _create_contact_row(client, project_name, "contacts", 0)

    resp = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "context": "contacts",
            "entries": {"contact_id": 0, "name": "impostor"},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400, resp.json()
    assert "Duplicate composite key" in resp.json()["detail"]


@pytest.mark.anyio
async def test_detach_delete_releases_context_claims(
    client: AsyncClient,
    dbsession: Session,
):
    """Removing a log from one context frees that context's keys."""
    project_name = "detach-releases-claims"
    await _create_project(client, project_name)
    await _create_keyed_context(client, project_name, "contacts")
    ctx_id = _context_id(dbsession, project_name, "contacts")

    log_id = await _create_contact_row(client, project_name, "contacts", 5)

    resp = await client.post(
        f"/v0/project/{project_name}/contexts/add_logs",
        json={"context_name": "archive", "log_ids": [log_id]},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    resp = await _delete_logs(
        client,
        [([log_id], None)],
        project_name=project_name,
        context="contacts",
    )
    assert resp.status_code == 200, resp.json()

    assert _contact_hash(5) not in _constraint_holders(dbsession, ctx_id)
    replacement = await _create_contact_row(client, project_name, "contacts", 5)
    assert _constraint_holders(dbsession, ctx_id)[_contact_hash(5)] == replacement

    resp = await client.get(
        f"/v0/logs?project_name={project_name}&context=archive",
        headers=HEADERS,
    )
    assert [log["id"] for log in resp.json()["logs"]] == [log_id]


@pytest.mark.anyio
async def test_full_delete_releases_claims_in_every_context(
    client: AsyncClient,
    dbsession: Session,
):
    """Hard-deleting a log drops its claims beyond the deleting context."""
    project_name = "full-delete-releases-claims"
    await _create_project(client, project_name)
    await _create_keyed_context(client, project_name, "contacts")
    await _create_keyed_context(client, project_name, "mirror")
    ctx_id = _context_id(dbsession, project_name, "contacts")
    mirror_id = _context_id(dbsession, project_name, "mirror")

    log_id = await _create_contact_row(client, project_name, "contacts", 7)

    # A merge-style copied claim in another context, held by the same log.
    project_id = (
        dbsession.query(Project.id).filter(Project.name == project_name).scalar()
    )
    dbsession.execute(
        text(
            "INSERT INTO log_unique_constraint "
            "(context_id, project_id, field_name, value_hash, log_event_id) "
            "VALUES (:cid, :pid, :field, :vhash, :lid)",
        ),
        {
            "cid": mirror_id,
            "pid": project_id,
            "field": COMPOSITE_KEY_FIELD,
            "vhash": _contact_hash(7),
            "lid": log_id,
        },
    )
    dbsession.flush()

    resp = await _delete_logs(
        client,
        [([log_id], None)],
        project_name=project_name,
        context="contacts",
    )
    assert resp.status_code == 200, resp.json()

    assert _contact_hash(7) not in _constraint_holders(dbsession, ctx_id)
    assert _contact_hash(7) not in _constraint_holders(dbsession, mirror_id)


@pytest.mark.anyio
async def test_skip_mode_ignores_orphaned_constraint(
    client: AsyncClient,
    dbsession: Session,
):
    """on_duplicate=skip must not skip a key whose holder is gone."""
    project_name = "skip-ignores-orphans"
    await _create_project(client, project_name)
    await _create_keyed_context(client, project_name, "contacts")

    stale_id = await _create_contact_row(client, project_name, "contacts", 9)
    _orphan_log(dbsession, stale_id)

    resp = await client.post(
        "/v0/logs",
        json={
            "project_name": project_name,
            "context": "contacts",
            "on_duplicate": "skip",
            "entries": [
                {"contact_id": 9, "name": "healed"},
                {"contact_id": 10, "name": "fresh"},
            ],
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert len(body["log_event_ids"]) == 2
    assert not body.get("failed")


@pytest.mark.anyio
async def test_context_delete_drops_constraint_rows(
    client: AsyncClient,
    dbsession: Session,
):
    """Deleting a context leaves no uniqueness rows scoped to it."""
    project_name = "context-delete-drops-claims"
    await _create_project(client, project_name)
    await _create_keyed_context(client, project_name, "contacts")
    ctx_id = _context_id(dbsession, project_name, "contacts")
    await _create_contact_row(client, project_name, "contacts", 3)

    resp = await client.delete(
        f"/v0/project/{project_name}/contexts/contacts",
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    assert _constraint_holders(dbsession, ctx_id) == {}
