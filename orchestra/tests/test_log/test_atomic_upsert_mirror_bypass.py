"""Atomic upsert archive mirroring for shared-space context paths."""

from __future__ import annotations

import logging
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Context, LogEventContext, Project
from orchestra.tests.test_log import HEADERS, _create_project


def _project_name(label: str) -> str:
    """Return a project name that is isolated across repeated test runs."""

    return f"atomic-mirror-{label}-{uuid.uuid4().hex}"


async def _atomic_upsert(
    client: AsyncClient,
    *,
    project_name: str,
    context_name: str,
    row_id: str,
    add_to_all_context: bool,
):
    """Drive the public atomic-upsert endpoint with a stable row identity."""

    return await client.post(
        "/v0/logs/atomic",
        json={
            "project": project_name,
            "context": context_name,
            "unique_keys": {"row_id": "str"},
            "field": "value",
            "operation": "+1",
            "initial_data": {
                "row_id": row_id,
                "_user": context_name.split("/")[0],
                "_assistant": context_name.split("/")[1],
            },
            "add_to_all_context": add_to_all_context,
        },
        headers=HEADERS,
    )


def _context_links_for_log(
    dbsession: Session,
    *,
    project_name: str,
    log_id: int,
) -> list[str]:
    """Return every context name linked to a log in one project."""

    dbsession.expire_all()
    project = dbsession.query(Project).filter(Project.name == project_name).one()
    rows = (
        dbsession.query(Context.name)
        .join(LogEventContext, LogEventContext.context_id == Context.id)
        .filter(
            Context.project_id == project.id,
            LogEventContext.log_event_id == log_id,
        )
        .order_by(Context.name)
        .all()
    )
    return [row[0] for row in rows]


def _context_exists(
    dbsession: Session,
    *,
    project_name: str,
    context_name: str,
) -> bool:
    """Return whether a context exists in one project."""

    dbsession.expire_all()
    project = dbsession.query(Project).filter(Project.name == project_name).one()
    return (
        dbsession.query(Context)
        .filter(Context.project_id == project.id, Context.name == context_name)
        .one_or_none()
        is not None
    )


@pytest.mark.anyio
async def test_personal_path_mirrors_when_add_to_all_context_true(
    client: AsyncClient,
    dbsession: Session,
):
    """Personal atomic upserts mirror into the archive context when requested."""

    project_name = _project_name("personal")
    await _create_project(client, project_name)

    response = await _atomic_upsert(
        client,
        project_name=project_name,
        context_name="user1/assistant1/SomeTable",
        row_id="personal-mirror",
        add_to_all_context=True,
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["mirrored_contexts"] == ["All/SomeTable"]
    assert _context_links_for_log(
        dbsession,
        project_name=project_name,
        log_id=payload["log_id"],
    ) == ["All/SomeTable", "user1/assistant1/SomeTable"]


@pytest.mark.anyio
async def test_space_path_skips_mirror_when_add_to_all_context_true(
    client: AsyncClient,
    dbsession: Session,
    caplog: pytest.LogCaptureFixture,
):
    """Shared-space atomic upserts keep only the canonical context link."""

    project_name = _project_name("space")
    await _create_project(client, project_name)
    caplog.set_level(logging.DEBUG, logger="orchestra.web.api.log.views")

    response = await _atomic_upsert(
        client,
        project_name=project_name,
        context_name="Spaces/7/SomeTable",
        row_id="space-bypass",
        add_to_all_context=True,
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["mirrored_contexts"] is None
    assert _context_links_for_log(
        dbsession,
        project_name=project_name,
        log_id=payload["log_id"],
    ) == ["Spaces/7/SomeTable"]
    assert not _context_exists(
        dbsession,
        project_name=project_name,
        context_name="All/SomeTable",
    )
    assert any(
        getattr(record, "mirror_skipped", False) is True
        and getattr(record, "context_name", None) == "Spaces/7/SomeTable"
        for record in caplog.records
    )


@pytest.mark.anyio
async def test_space_path_idempotent_under_retry(
    client: AsyncClient,
    dbsession: Session,
):
    """Retries against a shared-space row update the canonical row without mirroring."""

    project_name = _project_name("retry")
    await _create_project(client, project_name)

    first_response = await _atomic_upsert(
        client,
        project_name=project_name,
        context_name="Spaces/7/SomeTable",
        row_id="space-retry",
        add_to_all_context=True,
    )
    second_response = await _atomic_upsert(
        client,
        project_name=project_name,
        context_name="Spaces/7/SomeTable",
        row_id="space-retry",
        add_to_all_context=True,
    )

    assert first_response.status_code == 200, first_response.json()
    assert second_response.status_code == 200, second_response.json()
    first_payload = first_response.json()
    second_payload = second_response.json()
    assert first_payload["created"] is True
    assert second_payload["created"] is False
    assert second_payload["log_id"] == first_payload["log_id"]
    assert second_payload["new_value"] == 2.0
    assert second_payload["mirrored_contexts"] is None
    assert _context_links_for_log(
        dbsession,
        project_name=project_name,
        log_id=second_payload["log_id"],
    ) == ["Spaces/7/SomeTable"]
    assert not _context_exists(
        dbsession,
        project_name=project_name,
        context_name="All/SomeTable",
    )


@pytest.mark.anyio
async def test_personal_path_does_not_mirror_when_add_to_all_context_false(
    client: AsyncClient,
    dbsession: Session,
):
    """Personal atomic upserts do not mirror when archive mirroring is not requested."""

    project_name = _project_name("control")
    await _create_project(client, project_name)

    response = await _atomic_upsert(
        client,
        project_name=project_name,
        context_name="user1/assistant1/SomeTable",
        row_id="personal-control",
        add_to_all_context=False,
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["mirrored_contexts"] is None
    assert _context_links_for_log(
        dbsession,
        project_name=project_name,
        log_id=payload["log_id"],
    ) == ["user1/assistant1/SomeTable"]
    assert not _context_exists(
        dbsession,
        project_name=project_name,
        context_name="All/SomeTable",
    )
