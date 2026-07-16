"""Fixture-backed provider trigger catalog import service tests."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.trigger_catalog_dao import TriggerCatalogDAO
from orchestra.provider_triggers.backend_ids import (
    COMPOSIO_BACKEND_ID,
    PIPEDREAM_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.services.trigger_catalog_import_service import (
    TriggerCatalogImportService,
)


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
