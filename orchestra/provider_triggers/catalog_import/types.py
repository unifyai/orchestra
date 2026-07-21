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
    # Capability matrix (native honest bar). ``config_schema`` encodes the
    # target-resource family the user/twin must supply (empty ``{}`` when no
    # config is required). ``live_ready`` marks a slug as part of this
    # milestone's live turn-on set; ``delivery_only`` marks output-only slugs
    # (e.g. Google Chat ``*.batch*``) that can arrive on a base subscription but
    # are never standalone enable targets. ``None`` means the backend does not
    # participate in the native capability gate (Composio/Pipedream).
    config_schema: dict[str, Any] = field(default_factory=dict)
    live_ready: bool | None = None
    delivery_only: bool | None = None
    target_resource_family: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def provisionable(self) -> bool | None:
        """True when a slug is a standalone, live enable target.

        ``None`` when the backend does not carry native capability metadata,
        so non-native backends are never gated by the honest-bar checks.
        """

        if self.live_ready is None and self.delivery_only is None:
            return None
        return bool(self.live_ready) and not bool(self.delivery_only)

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
            "config_schema": self.config_schema,
            "live_ready": self.live_ready,
            "delivery_only": self.delivery_only,
            "target_resource_family": self.target_resource_family,
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
    config_schema = payload.get("config_schema")
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
        config_schema=dict(config_schema) if isinstance(config_schema, dict) else {},
        live_ready=(
            bool(payload["live_ready"])
            if payload.get("live_ready") is not None
            else None
        ),
        delivery_only=(
            bool(payload["delivery_only"])
            if payload.get("delivery_only") is not None
            else None
        ),
        target_resource_family=(
            str(payload["target_resource_family"])
            if payload.get("target_resource_family") is not None
            else None
        ),
        raw_metadata=dict(payload.get("raw_metadata") or {}),
    )
