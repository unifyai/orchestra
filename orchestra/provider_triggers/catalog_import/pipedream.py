"""Pipedream trigger catalog importer."""

from __future__ import annotations

import os

from orchestra.provider_triggers.backend_ids import PIPEDREAM_BACKEND_ID
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.provider_triggers.catalog_import.types import ProviderTriggerCatalogEntry


def _fixture_import_allowed(environment: str) -> bool:
    return environment == "selfhost"


def _pipedream_app_hint(component: dict[str, object]) -> str | None:
    for prop in component.get("configurable_props") or []:
        if not isinstance(prop, dict):
            continue
        if prop.get("type") == "app" and prop.get("app"):
            return str(prop["app"])
    component_key = str(component.get("key") or "").strip()
    if "-" in component_key:
        return component_key.split("-", 1)[0]
    return None


class PipedreamTriggerCatalogImporter:
    """Import Pipedream trigger components for staging."""

    backend_id = PIPEDREAM_BACKEND_ID

    def __init__(self, *, environment: str = "selfhost") -> None:
        self.environment = environment

    def list_trigger_catalog_entries(self) -> list[ProviderTriggerCatalogEntry]:
        if not all(
            os.getenv(name, "").strip()
            for name in (
                "PIPEDREAM_CLIENT_ID",
                "PIPEDREAM_CLIENT_SECRET",
                "PIPEDREAM_PROJECT_ID",
            )
        ):
            if not _fixture_import_allowed(self.environment):
                raise RuntimeError(
                    "Pipedream Connect credentials are required for non-selfhost "
                    "catalog import",
                )
            _, entries = load_fixture_catalog(self.backend_id)
            return entries

        from orchestra.integrations.providers.pipedream import PipedreamProviderAdapter

        adapter = PipedreamProviderAdapter()
        components = adapter.list_components(component_type="trigger")
        entries: list[ProviderTriggerCatalogEntry] = []
        for component in components:
            component_key = str(
                component.get("key") or component.get("name") or "",
            ).strip()
            if not component_key:
                continue
            entries.append(
                ProviderTriggerCatalogEntry(
                    backend_id=self.backend_id,
                    provider_trigger_slug=component_key,
                    provider_version=(
                        str(component.get("version"))
                        if component.get("version") is not None
                        else None
                    ),
                    canonical_app_hint=_pipedream_app_hint(component),
                    raw_metadata=component,
                ),
            )
        return sorted(entries, key=lambda item: item.provider_trigger_slug)
