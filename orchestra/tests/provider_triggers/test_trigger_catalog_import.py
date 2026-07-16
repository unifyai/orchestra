"""Fixture-backed provider trigger catalog import service tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.trigger_catalog_dao import TriggerCatalogDAO
from orchestra.provider_triggers.backend_ids import (
    COMPOSIO_BACKEND_ID,
    PIPEDREAM_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import.composio import (
    ComposioTriggerCatalogImporter,
)
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.services.trigger_catalog_import_service import (
    TriggerCatalogImportService,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")



@pytest.mark.parametrize(
    ("backend_id", "credential_envs"),
    [
        (COMPOSIO_BACKEND_ID, ("COMPOSIO_API_KEY",)),
        (
            PIPEDREAM_BACKEND_ID,
            (
                "PIPEDREAM_CLIENT_ID",
                "PIPEDREAM_CLIENT_SECRET",
                "PIPEDREAM_PROJECT_ID",
            ),
        ),
    ],
)
def test_import_catalog_uses_fixture_entries_without_provider_credentials(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
    backend_id: str,
    credential_envs: tuple[str, ...],
) -> None:
    for env_name in credential_envs:
        monkeypatch.delenv(env_name, raising=False)

    expected_version, expected_entries = load_fixture_catalog(backend_id)
    service = TriggerCatalogImportService(dbsession)

    first = service.import_catalog(backend_id=backend_id, environment="selfhost")
    second = service.import_catalog(backend_id=backend_id, environment="selfhost")

    dao = TriggerCatalogDAO(dbsession)
    bootstrap = dao.get_bootstrap_state(environment="selfhost", backend_id=backend_id)
    assert bootstrap is not None

    snapshot = dao.get_snapshot_by_hash(
        environment="selfhost",
        backend_id=backend_id,
        content_hash=first.content_hash,
    )
    assert snapshot is not None

    candidates = dao.list_candidates_for_snapshot(snapshot.id)
    assert first.skipped is False
    assert first.catalog_version == expected_version
    assert first.entry_count == len(expected_entries)
    assert [row.provider_trigger_slug for row in candidates] == sorted(
        entry.provider_trigger_slug for entry in expected_entries
    )
    assert bootstrap.desired_hash == first.content_hash

    assert second.skipped is True
    assert second.content_hash == first.content_hash


def test_composio_live_catalog_import_uses_rest_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-composio-key")
    calls: list[dict[str, Any]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        assert method == "GET"
        assert url.endswith("/triggers_types")
        calls.append(kwargs)
        params = kwargs.get("params") or {}
        if params.get("cursor") == "page-2":
            return _FakeResponse(
                payload={
                    "items": [
                        {
                            "slug": "GMAIL_NEW_GMAIL_MESSAGE",
                            "version": "2",
                            "toolkit": {"slug": "gmail"},
                            "config": {"type": "object"},
                            "name": "New Gmail Message",
                        },
                    ],
                    "next_cursor": None,
                },
            )
        return _FakeResponse(
            payload={
                "items": [
                    {
                        "slug": "GITHUB_ISSUE_CREATED_TRIGGER",
                        "version": "1",
                        "toolkit": {"slug": "github"},
                        "config": {"type": "object"},
                        "name": "Issue Created",
                    },
                ],
                "next_cursor": "page-2",
            },
        )

    importer = ComposioTriggerCatalogImporter(
        environment="staging",
        request_fn=request_fn,
        base_url="https://backend.composio.dev/api/v3.1",
    )
    entries = importer.list_trigger_catalog_entries()

    assert [entry.provider_trigger_slug for entry in entries] == [
        "GITHUB_ISSUE_CREATED_TRIGGER",
        "GMAIL_NEW_GMAIL_MESSAGE",
    ]
    assert entries[0].canonical_app_hint == "github"
    assert entries[1].canonical_app_hint == "gmail"
    assert calls[0]["headers"]["x-api-key"] == "test-composio-key"
    assert calls[0]["params"]["limit"] == 1000
    assert calls[1]["params"]["cursor"] == "page-2"
