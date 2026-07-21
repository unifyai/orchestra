"""Google Workspace Events catalog importer."""

from __future__ import annotations

import os
from typing import Any, Callable

from orchestra.provider_triggers.backend_ids import NATIVE_GOOGLE_BACKEND_ID
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.provider_triggers.catalog_import.native_manifest import (
    load_native_catalog_entries,
)
from orchestra.provider_triggers.catalog_import.types import ProviderTriggerCatalogEntry


def _fixture_import_allowed(environment: str) -> bool:
    return environment == "selfhost"


class NativeGoogleTriggerCatalogImporter:
    """Import Google Workspace Events types for staging."""

    backend_id = NATIVE_GOOGLE_BACKEND_ID

    def __init__(
        self,
        *,
        environment: str = "selfhost",
        request_fn: Callable[..., Any] | None = None,
        force_fixture: bool | None = None,
    ) -> None:
        self.environment = environment
        self._request_fn = request_fn
        if force_fixture is None:
            force_fixture = os.getenv(
                "NATIVE_GOOGLE_CATALOG_FORCE_FIXTURE",
                "",
            ).strip().lower() in {"1", "true", "yes"}
        self._force_fixture = force_fixture

    def list_trigger_catalog_entries(self) -> list[ProviderTriggerCatalogEntry]:
        if self._force_fixture:
            if not _fixture_import_allowed(self.environment):
                raise RuntimeError(
                    "NATIVE_GOOGLE_CATALOG_FORCE_FIXTURE is only allowed in selfhost",
                )
            _, entries = load_fixture_catalog(self.backend_id)
            return entries

        try:
            _, entries = load_native_catalog_entries(
                self.backend_id,
                request_fn=self._request_fn,
            )
            return entries
        except FileNotFoundError:
            if not _fixture_import_allowed(self.environment):
                raise RuntimeError(
                    "Native Google catalog manifest missing for non-selfhost import",
                ) from None
            _, entries = load_fixture_catalog(self.backend_id)
            return entries
