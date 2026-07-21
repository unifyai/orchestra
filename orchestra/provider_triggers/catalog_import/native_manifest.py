"""Load native provider trigger catalogs from manifests or remote URLs."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from orchestra.provider_triggers.backend_ids import (
    NATIVE_GOOGLE_BACKEND_ID,
    NATIVE_MICROSOFT_BACKEND_ID,
)
from orchestra.provider_triggers.catalog_import.types import (
    ProviderTriggerCatalogEntry,
    entry_from_mapping,
)

MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"

_NATIVE_SIGNATURE_DEFAULTS = {
    "retry_stable_identity_field": "event_id",
    "signature_headers": list(
        (
            "x-unify-webhook-id",
            "x-unify-webhook-timestamp",
            "x-unify-webhook-signature",
        ),
    ),
    "signature_scheme": "native_hmac_sha256_timestamp_dot_body",
    "timestamp_tolerance_seconds": 300,
    "provisioning_idempotency_supported": True,
}


def native_catalog_url_env(backend_id: str) -> str:
    if backend_id == NATIVE_GOOGLE_BACKEND_ID:
        return "NATIVE_GOOGLE_CATALOG_URL"
    if backend_id == NATIVE_MICROSOFT_BACKEND_ID:
        return "NATIVE_MICROSOFT_CATALOG_URL"
    raise LookupError(f"No native catalog URL env for {backend_id!r}")


def load_native_catalog_payload(
    backend_id: str,
    *,
    request_fn: Any | None = None,
) -> dict[str, Any]:
    """Load one native catalog manifest from URL override or bundled file."""

    url = os.getenv(native_catalog_url_env(backend_id), "").strip()
    if url:
        return _fetch_catalog_payload(url, request_fn=request_fn)
    manifest_path = MANIFEST_DIR / f"{backend_id}_workspace_events.json"
    if backend_id == NATIVE_MICROSOFT_BACKEND_ID:
        manifest_path = MANIFEST_DIR / f"{backend_id}_graph_subscriptions.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def catalog_entries_from_payload(
    backend_id: str,
    payload: Mapping[str, Any],
) -> list[ProviderTriggerCatalogEntry]:
    """Normalize one native catalog manifest into staging entries."""

    entries: list[ProviderTriggerCatalogEntry] = []
    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        merged = dict(_NATIVE_SIGNATURE_DEFAULTS)
        merged.update(item)
        merged.setdefault("raw_metadata", {})
        if isinstance(merged["raw_metadata"], dict):
            merged["raw_metadata"].setdefault("source", "native_manifest")
        entries.append(entry_from_mapping(backend_id, merged))
    return sorted(entries, key=lambda row: row.provider_trigger_slug)


def load_native_catalog_entries(
    backend_id: str,
    *,
    request_fn: Any | None = None,
) -> tuple[str, list[ProviderTriggerCatalogEntry]]:
    payload = load_native_catalog_payload(backend_id, request_fn=request_fn)
    catalog_version = str(payload.get("catalog_version") or "native-live-v1")
    return catalog_version, catalog_entries_from_payload(backend_id, payload)


def _fetch_catalog_payload(
    url: str,
    *,
    request_fn: Any | None = None,
) -> dict[str, Any]:
    if request_fn is not None:
        response = request_fn(url)
        if isinstance(response, dict):
            return response
        if hasattr(response, "json"):
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        raise ValueError(f"Native catalog URL {url!r} did not return a JSON object")

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to fetch native catalog from {url!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Native catalog URL {url!r} did not return a JSON object")
    return payload
