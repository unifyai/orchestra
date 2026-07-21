"""Import provider trigger catalogs into hashed staging snapshots."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.dao.trigger_catalog_dao import TriggerCatalogDAO
from orchestra.provider_triggers.backend_ids import (
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_MICROSOFT_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import import (
    compute_catalog_content_hash,
    get_trigger_catalog_importer,
)
from orchestra.provider_triggers.catalog_import.fixtures import load_fixture_catalog
from orchestra.provider_triggers.catalog_import.native_manifest import (
    load_native_catalog_entries,
)
from orchestra.provider_triggers.catalog_types import TriggerCatalogImportStatus


@dataclass(frozen=True)
class TriggerCatalogImportResult:
    """Outcome of one provider trigger catalog import."""

    backend_id: str
    environment: str
    skipped: bool
    content_hash: str
    catalog_version: str
    entry_count: int
    snapshot_id: int | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class TriggerCatalogImportService:
    """Stage provider trigger catalogs for connection-gated discovery."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.dao = TriggerCatalogDAO(session)

    def import_catalog(
        self,
        *,
        backend_id: str,
        environment: str = "selfhost",
    ) -> TriggerCatalogImportResult:
        bootstrap = self.dao.get_or_create_bootstrap_state(
            environment=environment,
            backend_id=backend_id,
        )
        try:
            importer = get_trigger_catalog_importer(
                backend_id,
                environment=environment,
            )
            entries = importer.list_trigger_catalog_entries()
            catalog_version = self._resolve_catalog_version(
                backend_id,
                entries,
                environment=environment,
            )
            content_hash = compute_catalog_content_hash(entries)
        except Exception as exc:
            bootstrap.last_status = TriggerCatalogImportStatus.failed.value
            bootstrap.last_error = str(exc)
            bootstrap.last_import_diagnostics_json = {
                "failed": True,
                "error": str(exc),
            }
            self.session.flush()
            raise

        if bootstrap.desired_hash == content_hash:
            bootstrap.last_status = TriggerCatalogImportStatus.skipped.value
            bootstrap.last_import_diagnostics_json = {
                "skipped": True,
                "content_hash": content_hash,
                "entry_count": len(entries),
            }
            self.session.flush()
            return TriggerCatalogImportResult(
                backend_id=backend_id,
                environment=environment,
                skipped=True,
                content_hash=content_hash,
                catalog_version=catalog_version,
                entry_count=len(entries),
                diagnostics=bootstrap.last_import_diagnostics_json,
            )

        existing_snapshot = self.dao.get_snapshot_by_hash(
            environment=environment,
            backend_id=backend_id,
            content_hash=content_hash,
        )
        if existing_snapshot is not None:
            snapshot = existing_snapshot
        else:
            snapshot = self.dao.create_snapshot(
                environment=environment,
                backend_id=backend_id,
                catalog_version=catalog_version,
                content_hash=content_hash,
                raw_entry_count=len(entries),
            )
            self.dao.insert_candidates(snapshot_id=snapshot.id, entries=entries)

        now = datetime.now(timezone.utc)
        bootstrap.desired_hash = content_hash
        bootstrap.last_status = TriggerCatalogImportStatus.imported.value
        bootstrap.last_error = None
        bootstrap.candidates_imported = len(entries)
        bootstrap.last_imported_at = now
        bootstrap.last_import_diagnostics_json = {
            "content_hash": content_hash,
            "catalog_version": catalog_version,
            "entry_count": len(entries),
            "snapshot_id": snapshot.id,
        }
        self.session.flush()
        return TriggerCatalogImportResult(
            backend_id=backend_id,
            environment=environment,
            skipped=False,
            content_hash=content_hash,
            catalog_version=catalog_version,
            entry_count=len(entries),
            snapshot_id=snapshot.id,
            diagnostics=bootstrap.last_import_diagnostics_json,
        )

    @staticmethod
    def _resolve_catalog_version(
        backend_id: str,
        entries: list[Any],
        *,
        environment: str,
    ) -> str:
        if (
            environment == "selfhost"
            and backend_id
            not in {NATIVE_GOOGLE_BACKEND_ID, NATIVE_MICROSOFT_BACKEND_ID}
            and not os.getenv(
                {
                    "composio": "COMPOSIO_API_KEY",
                    "pipedream": "PIPEDREAM_CLIENT_ID",
                }.get(backend_id, ""),
                "",
            ).strip()
        ):
            try:
                catalog_version, _ = load_fixture_catalog(backend_id)
                return catalog_version
            except FileNotFoundError:
                pass
        if backend_id in {NATIVE_GOOGLE_BACKEND_ID, NATIVE_MICROSOFT_BACKEND_ID}:
            try:
                catalog_version, _ = load_native_catalog_entries(backend_id)
                return catalog_version
            except FileNotFoundError:
                try:
                    catalog_version, _ = load_fixture_catalog(backend_id)
                    return catalog_version
                except FileNotFoundError:
                    pass
        if not entries:
            return "empty"
        versions = [
            entry.provider_version
            for entry in entries
            if getattr(entry, "provider_version", None)
        ]
        if versions:
            return versions[0] or "live"
        return "live"
