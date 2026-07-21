"""Stable provider-event identity extraction for ingress and probes."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping


def provider_account_subject_hmac(subject: str, *, pepper: str | bytes) -> str:
    """Return the durable HMAC digest for one provider-account subject."""

    key = pepper.encode("utf-8") if isinstance(pepper, str) else pepper
    return hmac.new(key, subject.encode("utf-8"), hashlib.sha256).hexdigest()


def composio_v3_event_identity(payload: Mapping[str, Any]) -> str | None:
    """Return Composio V3 top-level event id when present."""

    event_id = payload.get("id")
    if isinstance(event_id, str) and event_id.strip():
        return event_id.strip()
    return None


def pipedream_delivery_identity(
    headers: Mapping[str, str],
    body: Mapping[str, Any],
) -> str | None:
    """Return a retry-stable Pipedream delivery identity when available."""

    for header_name in ("x-pd-event-id", "x-pd-delivery-id", "x-pd-request-id"):
        value = headers.get(header_name) or headers.get(header_name.upper())
        if isinstance(value, str) and value.strip():
            return value.strip()

    for field in ("id", "event_id", "delivery_id", "trace_id"):
        candidate = body.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def native_event_identity(payload: Mapping[str, Any]) -> str | None:
    """Return retry-stable native trigger event id when present."""

    for field in ("event_id", "id", "resource_id"):
        candidate = payload.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    data = payload.get("data")
    if isinstance(data, Mapping):
        for field in ("event_id", "id", "resource_id", "transcript_file_id"):
            candidate = data.get(field)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def binding_event_identity_hmac(
    *,
    binding_hmac_key: bytes,
    provider_event_identity: str,
) -> str:
    """Compute the immutable per-binding event identity digest."""

    digest = hmac.new(
        binding_hmac_key,
        provider_event_identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable copy with obvious secret fields removed."""

    sensitive_tokens = (
        "secret",
        "token",
        "password",
        "authorization",
        "signature",
        "signing",
        "api_key",
    )
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        lowered = str(key).lower()
        if any(token in lowered for token in sensitive_tokens):
            redacted[key] = "<redacted>"
        elif isinstance(item, Mapping):
            redacted[key] = redact_mapping(item)
        elif isinstance(item, list):
            redacted[key] = [
                redact_mapping(entry) if isinstance(entry, Mapping) else entry
                for entry in item
            ]
        else:
            redacted[key] = item
    return redacted
