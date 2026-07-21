"""Registry for provider trigger catalog importers."""

from __future__ import annotations

from orchestra.provider_triggers.backend_ids import (
    COMPOSIO_BACKEND_ID,
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_MICROSOFT_BACKEND_ID,
    PIPEDREAM_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import.composio import (
    ComposioTriggerCatalogImporter,
)
from orchestra.provider_triggers.catalog_import.native_google import (
    NativeGoogleTriggerCatalogImporter,
)
from orchestra.provider_triggers.catalog_import.native_microsoft import (
    NativeMicrosoftTriggerCatalogImporter,
)
from orchestra.provider_triggers.catalog_import.pipedream import (
    PipedreamTriggerCatalogImporter,
)
from orchestra.provider_triggers.catalog_import.types import TriggerCatalogImporter

_TRIGGER_CATALOG_IMPORTERS: dict[str, type[TriggerCatalogImporter]] = {
    COMPOSIO_BACKEND_ID: ComposioTriggerCatalogImporter,
    PIPEDREAM_BACKEND_ID: PipedreamTriggerCatalogImporter,
    NATIVE_GOOGLE_BACKEND_ID: NativeGoogleTriggerCatalogImporter,
    NATIVE_MICROSOFT_BACKEND_ID: NativeMicrosoftTriggerCatalogImporter,
}


def get_trigger_catalog_importer(
    backend_id: str,
    *,
    environment: str = "selfhost",
) -> TriggerCatalogImporter:
    """Return the catalog importer for one supported backend."""

    importer_cls = _TRIGGER_CATALOG_IMPORTERS.get(backend_id)
    if importer_cls is None:
        raise LookupError(f"Unsupported trigger catalog backend {backend_id!r}")
    return importer_cls(environment=environment)


def supported_trigger_catalog_backends() -> tuple[str, ...]:
    """Return backend ids with catalog importers."""

    return tuple(sorted(_TRIGGER_CATALOG_IMPORTERS))
