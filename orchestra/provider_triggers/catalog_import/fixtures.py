"""Fixture-backed provider trigger catalog entries for local import tests."""

from __future__ import annotations

import json
from pathlib import Path

from orchestra.provider_triggers.catalog_import.types import (
    ProviderTriggerCatalogEntry,
    entry_from_mapping,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "provider_trigger_contract"
)


def load_fixture_catalog(
    backend_id: str,
) -> tuple[str, list[ProviderTriggerCatalogEntry]]:
    """Load one committed provider trigger catalog fixture."""

    fixture_path = FIXTURE_DIR / f"{backend_id}_trigger_catalog.fixture.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    catalog_version = str(payload["catalog_version"])
    entries = [entry_from_mapping(backend_id, item) for item in payload["entries"]]
    return catalog_version, entries
