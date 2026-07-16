"""Composio trigger catalog importer."""

from __future__ import annotations

import os
from typing import Any, Callable

from orchestra.provider_triggers.backend_ids import COMPOSIO_BACKEND_ID
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.provider_triggers.catalog_import.types import ProviderTriggerCatalogEntry
from orchestra.provider_triggers.composio_trigger_adapter import (
    DEFAULT_COMPOSIO_BASE_URL,
    list_composio_trigger_types,
)


def _fixture_import_allowed(environment: str) -> bool:
    return environment == "selfhost"


def _composio_toolkit_hint(toolkit: object) -> str | None:
    if isinstance(toolkit, dict):
        slug = str(toolkit.get("slug") or toolkit.get("name") or "").strip()
        return slug or None
    if toolkit:
        return str(toolkit)
    return None


def _entry_from_trigger_type(
    trigger_dump: dict[str, Any],
) -> ProviderTriggerCatalogEntry | None:
    slug = str(
        trigger_dump.get("slug")
        or trigger_dump.get("name")
        or trigger_dump.get("trigger_slug")
        or "",
    ).strip()
    if not slug:
        return None
    return ProviderTriggerCatalogEntry(
        backend_id=COMPOSIO_BACKEND_ID,
        provider_trigger_slug=slug,
        provider_version=(
            str(
                trigger_dump.get("version")
                or trigger_dump.get("toolkit_version"),
            )
            if trigger_dump.get("version") or trigger_dump.get("toolkit_version")
            else None
        ),
        canonical_app_hint=_composio_toolkit_hint(trigger_dump.get("toolkit")),
        raw_metadata=trigger_dump,
    )


class ComposioTriggerCatalogImporter:
    """Import Composio trigger types for staging."""

    backend_id = COMPOSIO_BACKEND_ID

    def __init__(
        self,
        *,
        environment: str = "selfhost",
        request_fn: Callable[..., Any] | None = None,
        base_url: str | None = None,
    ) -> None:
        self.environment = environment
        self._request_fn = request_fn
        self._base_url = base_url

    def list_trigger_catalog_entries(self) -> list[ProviderTriggerCatalogEntry]:
        api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
        if not api_key:
            if not _fixture_import_allowed(self.environment):
                raise RuntimeError(
                    "COMPOSIO_API_KEY is required for non-selfhost catalog import",
                )
            _, entries = load_fixture_catalog(self.backend_id)
            return entries

        trigger_types = list_composio_trigger_types(
            api_key=api_key,
            base_url=self._base_url or DEFAULT_COMPOSIO_BASE_URL,
            request_fn=self._request_fn,
        )
        entries: list[ProviderTriggerCatalogEntry] = []
        for trigger_dump in trigger_types:
            entry = _entry_from_trigger_type(trigger_dump)
            if entry is not None:
                entries.append(entry)
        return sorted(entries, key=lambda item: item.provider_trigger_slug)
