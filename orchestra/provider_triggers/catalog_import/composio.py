"""Composio trigger catalog importer."""

from __future__ import annotations

import os

from orchestra.provider_triggers.backend_ids import COMPOSIO_BACKEND_ID
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.provider_triggers.catalog_import.types import ProviderTriggerCatalogEntry


def _fixture_import_allowed(environment: str) -> bool:
    return environment == "selfhost"


def _composio_toolkit_hint(toolkit: object) -> str | None:
    if isinstance(toolkit, dict):
        slug = str(toolkit.get("slug") or toolkit.get("name") or "").strip()
        return slug or None
    if toolkit:
        return str(toolkit)
    return None


class ComposioTriggerCatalogImporter:
    """Import Composio trigger types for staging."""

    backend_id = COMPOSIO_BACKEND_ID

    def __init__(self, *, environment: str = "selfhost") -> None:
        self.environment = environment

    def list_trigger_catalog_entries(self) -> list[ProviderTriggerCatalogEntry]:
        if not os.getenv("COMPOSIO_API_KEY", "").strip():
            if not _fixture_import_allowed(self.environment):
                raise RuntimeError(
                    "COMPOSIO_API_KEY is required for non-selfhost catalog import",
                )
            _, entries = load_fixture_catalog(self.backend_id)
            return entries

        from composio import Composio

        composio = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
        entries: list[ProviderTriggerCatalogEntry] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, str] = {}
            if cursor:
                kwargs["cursor"] = cursor
            response = composio.triggers.list(**kwargs)
            for trigger_type in response.items:
                trigger_dump = (
                    trigger_type.model_dump()
                    if hasattr(trigger_type, "model_dump")
                    else dict(trigger_type)
                )
                slug = str(
                    trigger_dump.get("slug")
                    or trigger_dump.get("name")
                    or trigger_dump.get("trigger_slug")
                    or "",
                ).strip()
                if not slug:
                    continue
                entries.append(
                    ProviderTriggerCatalogEntry(
                        backend_id=self.backend_id,
                        provider_trigger_slug=slug,
                        provider_version=(
                            str(
                                trigger_dump.get("version")
                                or trigger_dump.get("toolkit_version"),
                            )
                            if trigger_dump.get("version")
                            or trigger_dump.get("toolkit_version")
                            else None
                        ),
                        canonical_app_hint=_composio_toolkit_hint(
                            trigger_dump.get("toolkit"),
                        ),
                        raw_metadata=trigger_dump,
                    ),
                )
            cursor = getattr(response, "next_cursor", None)
            if not cursor:
                break
        return sorted(entries, key=lambda item: item.provider_trigger_slug)
