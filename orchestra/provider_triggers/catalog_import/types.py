"""Normalized provider trigger catalog import entries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ProviderTriggerCatalogEntry:
    """One provider-declared trigger entry normalized for staging."""

    backend_id: str
    provider_trigger_slug: str
    provider_version: str | None = None
    canonical_app_hint: str | None = None
    retry_stable_identity_field: str | None = None
    signature_headers: tuple[str, ...] = ()
    signature_scheme: str | None = None
    timestamp_tolerance_seconds: int | None = None
    provisioning_idempotency_supported: bool | None = None
    required_scopes: tuple[str, ...] = ()
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def normalized_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable representation for hashing."""

        return {
            "backend_id": self.backend_id,
            "provider_trigger_slug": self.provider_trigger_slug,
            "provider_version": self.provider_version,
            "canonical_app_hint": self.canonical_app_hint,
            "retry_stable_identity_field": self.retry_stable_identity_field,
            "signature_headers": list(self.signature_headers),
            "signature_scheme": self.signature_scheme,
            "timestamp_tolerance_seconds": self.timestamp_tolerance_seconds,
            "provisioning_idempotency_supported": (
                self.provisioning_idempotency_supported
            ),
            "required_scopes": list(self.required_scopes),
        }

    def unit_hash(self) -> str:
        """Return a stable hash for one candidate entry."""

        payload = json.dumps(
            self.normalized_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TriggerCatalogImporter(Protocol):
    """Fetch provider trigger catalog entries for one backend."""

    backend_id: str

    def list_trigger_catalog_entries(self) -> list[ProviderTriggerCatalogEntry]:
        """Return normalized trigger catalog entries for staging."""


def compute_catalog_content_hash(entries: list[ProviderTriggerCatalogEntry]) -> str:
    """Hash a full imported catalog snapshot for idempotent re-import."""

    normalized = [
        entry.normalized_dict()
        for entry in sorted(
            entries,
            key=lambda item: item.provider_trigger_slug,
        )
    ]
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def entry_from_mapping(
    backend_id: str,
    payload: Mapping[str, Any],
) -> ProviderTriggerCatalogEntry:
    """Build one catalog entry from importer/provider JSON."""

    signature_headers = payload.get("signature_headers") or []
    required_scopes = payload.get("required_scopes") or []
    return ProviderTriggerCatalogEntry(
        backend_id=backend_id,
        provider_trigger_slug=str(payload["provider_trigger_slug"]),
        provider_version=(
            str(payload["provider_version"])
            if payload.get("provider_version") is not None
            else None
        ),
        canonical_app_hint=(
            str(payload["canonical_app_hint"])
            if payload.get("canonical_app_hint") is not None
            else None
        ),
        retry_stable_identity_field=(
            str(payload["retry_stable_identity_field"])
            if payload.get("retry_stable_identity_field") is not None
            else None
        ),
        signature_headers=tuple(str(item) for item in signature_headers),
        signature_scheme=(
            str(payload["signature_scheme"])
            if payload.get("signature_scheme") is not None
            else None
        ),
        timestamp_tolerance_seconds=(
            int(payload["timestamp_tolerance_seconds"])
            if payload.get("timestamp_tolerance_seconds") is not None
            else None
        ),
        provisioning_idempotency_supported=(
            bool(payload["provisioning_idempotency_supported"])
            if payload.get("provisioning_idempotency_supported") is not None
            else None
        ),
        required_scopes=tuple(str(item) for item in required_scopes),
        raw_metadata=dict(payload.get("raw_metadata") or {}),
    )
