"""Regression: log mutations must be durable before the HTTP response returns.

Production ``get_db_session`` commits only after the response body is sent, so a
real client can race a follow-up GET against an uncommitted write. ASGI test
clients finish dependency cleanup before returning, which hides that race.

These tests seed with the normal concurrent session (commit on exit), then
switch the dependency to *rollback* on exit. Log mutations then persist across
requests only if the endpoint itself (or an intentional mid-handler commit)
makes them durable before returning.

``LogEventDAO.bulk_update`` commits internally today; the update test forces
those DAO commits to flush-only so the endpoint-level commit is what must make
the write visible. Delete tests disable empty-field cleanup for the same reason
(field-type deletion also commits mid-handler).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from orchestra.conftest import TestAwareAsyncClient
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dependencies import get_db_session
from orchestra.tests.test_log import HEADERS, _create_log, _create_project


@contextmanager
def _rollback_on_exit(app: FastAPI) -> Generator[None, None, None]:
    SessionFactory: sessionmaker[Session] = app.state.db_session_factory
    previous = app.dependency_overrides[get_db_session]

    def get_session_rollback_on_exit() -> Generator[Session, None, None]:
        session: Session = SessionFactory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    app.dependency_overrides[get_db_session] = get_session_rollback_on_exit
    try:
        yield
    finally:
        app.dependency_overrides[get_db_session] = previous


@contextmanager
def _dao_commits_become_flushes() -> Generator[None, None, None]:
    """Rewrite in-DAO ``session.commit()`` calls to ``flush()`` for one request."""

    real_bulk_update = LogEventDAO.bulk_update
    real_apply_jsonb_patch = LogEventDAO.apply_jsonb_patch

    def _with_flush_instead_of_commit(method):
        def wrapper(self, *args, **kwargs):
            session = self.session
            real_commit = session.commit
            session.commit = session.flush  # type: ignore[method-assign]
            try:
                return method(self, *args, **kwargs)
            finally:
                session.commit = real_commit  # type: ignore[method-assign]

        return wrapper

    with (
        patch.object(
            LogEventDAO,
            "bulk_update",
            _with_flush_instead_of_commit(real_bulk_update),
        ),
        patch.object(
            LogEventDAO,
            "apply_jsonb_patch",
            _with_flush_instead_of_commit(real_apply_jsonb_patch),
        ),
    ):
        yield


async def _get_by_id(client: TestAwareAsyncClient, project_name: str, log_id: int):
    return await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "from_ids": str(log_id),
        },
        headers=HEADERS,
    )


@pytest.mark.anyio
async def test_create_logs_visible_without_dependency_exit_commit(
    client_concurrent: TestAwareAsyncClient,
    fastapi_app_concurrent: FastAPI,
):
    project = "commit-before-response-create"
    created_project = await _create_project(client_concurrent, project)
    assert created_project.status_code == 200, created_project.text

    with _rollback_on_exit(fastapi_app_concurrent):
        created = await _create_log(
            client_concurrent,
            project,
            entries={"name": "created-before-response", "enabled": False},
        )
        assert created.status_code == 200, created.text
        log_id = created.json()["log_event_ids"][0]

        fetched = await _get_by_id(client_concurrent, project, log_id)
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["count"] == 1
        assert body["logs"][0]["id"] == log_id
        assert body["logs"][0]["entries"]["enabled"] is False


@pytest.mark.anyio
async def test_update_logs_visible_without_dependency_exit_commit(
    client_concurrent: TestAwareAsyncClient,
    fastapi_app_concurrent: FastAPI,
):
    project = "commit-before-response-update"
    created_project = await _create_project(client_concurrent, project)
    assert created_project.status_code == 200, created_project.text

    created = await _create_log(
        client_concurrent,
        project,
        entries={"name": "before-update", "score": 1},
    )
    assert created.status_code == 200, created.text
    log_id = created.json()["log_event_ids"][0]

    with _rollback_on_exit(fastapi_app_concurrent):
        with _dao_commits_become_flushes():
            updated = await client_concurrent.put(
                "/v0/logs",
                json={
                    "project_name": project,
                    "logs": [log_id],
                    "entries": {"score": 99},
                    "overwrite": True,
                },
                headers=HEADERS,
            )
        assert updated.status_code == 200, updated.text

        fetched = await _get_by_id(client_concurrent, project, log_id)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["logs"][0]["entries"]["score"] == 99


@pytest.mark.anyio
async def test_delete_logs_visible_without_dependency_exit_commit(
    client_concurrent: TestAwareAsyncClient,
    fastapi_app_concurrent: FastAPI,
):
    project = "commit-before-response-delete"
    created_project = await _create_project(client_concurrent, project)
    assert created_project.status_code == 200, created_project.text

    created = await _create_log(
        client_concurrent,
        project,
        entries={"name": "to-delete"},
    )
    assert created.status_code == 200, created.text
    log_id = created.json()["log_event_ids"][0]

    with _rollback_on_exit(fastapi_app_concurrent):
        deleted = await client_concurrent.request(
            "DELETE",
            "/v0/logs",
            json={
                "project_name": project,
                "ids_and_fields": [[log_id, None]],
                # Field-type cleanup commits mid-handler; disable it so only the
                # endpoint-level commit can make the row deletion durable.
                "delete_empty_fields": False,
            },
            headers=HEADERS,
        )
        assert deleted.status_code == 200, deleted.text

        fetched = await _get_by_id(client_concurrent, project, log_id)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["count"] == 0
        assert fetched.json()["logs"] == []
