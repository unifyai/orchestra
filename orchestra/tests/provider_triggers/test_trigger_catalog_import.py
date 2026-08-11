"""Fixture-backed provider trigger catalog import service tests."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.trigger_catalog_dao import TriggerCatalogDAO
from orchestra.provider_triggers.backend_ids import (
    COMPOSIO_BACKEND_ID,
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_MICROSOFT_BACKEND_ID,
    PIPEDREAM_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import.composio import (
    ComposioTriggerCatalogImporter,
)
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.provider_triggers.catalog_import.native_google import (
    NativeGoogleTriggerCatalogImporter,
)
from orchestra.provider_triggers.catalog_import.native_manifest import (
    load_native_catalog_entries,
)
from orchestra.provider_triggers.catalog_import.types import ProviderTriggerCatalogEntry
from orchestra.services.trigger_catalog_import_service import (
    TriggerCatalogImportService,
)


def _expected_catalog(backend_id: str) -> tuple[str, list]:
    if backend_id in {NATIVE_GOOGLE_BACKEND_ID, NATIVE_MICROSOFT_BACKEND_ID}:
        return load_native_catalog_entries(backend_id)
    return load_fixture_catalog(backend_id)


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
        (NATIVE_GOOGLE_BACKEND_ID, ()),
        (NATIVE_MICROSOFT_BACKEND_ID, ()),
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

    expected_version, expected_entries = _expected_catalog(backend_id)
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
    assert sorted(row.provider_trigger_slug for row in candidates) == sorted(
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


def test_native_google_catalog_importer_honors_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "catalog_version": "remote-google-v9",
        "entries": [
            {
                "provider_trigger_slug": "google.workspace.meet.transcript.v2.fileGenerated",
                "provider_version": "1",
                "canonical_app_hint": "google_meet",
                "raw_metadata": {"name": "Remote Meet transcript"},
            },
        ],
    }

    def request_fn(url: str) -> dict[str, object]:
        assert url == "https://example.test/native-google-catalog.json"
        return payload

    monkeypatch.setenv(
        "NATIVE_GOOGLE_CATALOG_URL",
        "https://example.test/native-google-catalog.json",
    )
    importer = NativeGoogleTriggerCatalogImporter(
        environment="staging",
        request_fn=request_fn,
    )
    entries = importer.list_trigger_catalog_entries()

    assert len(entries) == 1
    assert entries[0].provider_trigger_slug == (
        "google.workspace.meet.transcript.v2.fileGenerated"
    )


def test_native_google_catalog_importer_force_fixture_in_selfhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NATIVE_GOOGLE_CATALOG_FORCE_FIXTURE", "1")
    _, expected_entries = load_fixture_catalog(NATIVE_GOOGLE_BACKEND_ID)
    importer = NativeGoogleTriggerCatalogImporter(environment="selfhost")
    entries = importer.list_trigger_catalog_entries()
    assert len(entries) == len(expected_entries)


def test_list_candidates_for_snapshot_pages_without_duplicates_or_gaps(
    dbsession: Session,
) -> None:
    dao = TriggerCatalogDAO(dbsession)
    snapshot, _ = dao.get_or_create_snapshot(
        environment="selfhost",
        backend_id=COMPOSIO_BACKEND_ID,
        catalog_version="pagination-test",
        content_hash=f"pagination-{uuid.uuid4().hex}",
        raw_entry_count=5,
    )
    entries = [
        ProviderTriggerCatalogEntry(
            backend_id=COMPOSIO_BACKEND_ID,
            provider_trigger_slug=f"TRIGGER_{i:02d}",
            canonical_app_hint="github",
        )
        for i in range(5)
    ]
    dao.insert_candidates(snapshot_id=snapshot.id, entries=entries)

    unpaginated = dao.list_candidates_for_snapshot(snapshot.id)
    assert [row.provider_trigger_slug for row in unpaginated] == sorted(
        entry.provider_trigger_slug for entry in entries
    )

    page_size = 2
    paged_slugs: list[str] = []
    offset = 0
    while True:
        page = dao.list_candidates_for_snapshot(
            snapshot.id,
            limit=page_size,
            offset=offset,
        )
        if not page:
            break
        assert len(page) <= page_size
        paged_slugs.extend(row.provider_trigger_slug for row in page)
        offset += page_size

    assert paged_slugs == [row.provider_trigger_slug for row in unpaginated]


def test_import_survives_bootstrap_state_insert_race(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A losing concurrent first import recovers instead of failing.

    Simulates the loser's stale lookup: the bootstrap row already exists, but
    ``get_bootstrap_state`` misses it once, so the insert hits the unique
    constraint exactly as it does when two importers race on first boot. The
    conflict must be a no-op followed by a re-read, not an error.
    """

    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    service = TriggerCatalogImportService(dbsession)
    first = service.import_catalog(
        backend_id=COMPOSIO_BACKEND_ID,
        environment="selfhost",
    )

    real_get = TriggerCatalogDAO.get_bootstrap_state
    lookups = {"count": 0}

    def stale_once(self: TriggerCatalogDAO, *, environment: str, backend_id: str):
        lookups["count"] += 1
        if lookups["count"] == 1:
            return None
        return real_get(self, environment=environment, backend_id=backend_id)

    monkeypatch.setattr(TriggerCatalogDAO, "get_bootstrap_state", stale_once)

    second = service.import_catalog(
        backend_id=COMPOSIO_BACKEND_ID,
        environment="selfhost",
    )

    assert second.skipped is True
    assert second.content_hash == first.content_hash
    assert lookups["count"] >= 2


def test_import_survives_snapshot_insert_race(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A losing concurrent import reuses the winner's staged snapshot.

    A snapshot for this content hash already exists (the winner's), but this
    importer's bootstrap state does not yet desire it — the loser's exact
    position in the race. The snapshot insert must conflict into a reuse
    without duplicating candidates.
    """

    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    service = TriggerCatalogImportService(dbsession)
    dao = TriggerCatalogDAO(dbsession)
    first = service.import_catalog(
        backend_id=COMPOSIO_BACKEND_ID,
        environment="selfhost",
    )
    assert first.snapshot_id is not None
    candidates_before = len(dao.list_candidates_for_snapshot(first.snapshot_id))
    assert candidates_before > 0

    bootstrap = dao.get_bootstrap_state(
        environment="selfhost",
        backend_id=COMPOSIO_BACKEND_ID,
    )
    assert bootstrap is not None
    bootstrap.desired_hash = ""
    dbsession.flush()

    second = service.import_catalog(
        backend_id=COMPOSIO_BACKEND_ID,
        environment="selfhost",
    )

    assert second.skipped is False
    assert second.snapshot_id == first.snapshot_id
    candidates_after = len(dao.list_candidates_for_snapshot(first.snapshot_id))
    assert candidates_after == candidates_before
