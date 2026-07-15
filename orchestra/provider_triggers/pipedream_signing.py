"""Pipedream webhook signature verification and test signing helpers."""

from __future__ import annotations

import hashlib
import hmac
import time


def verify_pipedream_signature(
    *,
    signing_key: str,
    raw_body: bytes,
    signature_header: str,
    tolerance_seconds: int = 300,
    now_seconds: int | None = None,
) -> bool:
    """Validate an ``x-pd-signature`` header against the documented scheme."""

    if not signing_key or not signature_header:
        return False
    parts: dict[str, str] = {}
    for segment in signature_header.split(","):
        key, _, value = segment.partition("=")
        parts[key.strip()] = value.strip()
    timestamp = parts.get("t")
    provided = parts.get("v1")
    if not timestamp or not provided:
        return False
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False
    current = int(time.time() if now_seconds is None else now_seconds)
    if tolerance_seconds > 0 and abs(current - timestamp_int) > tolerance_seconds:
        return False
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(
        signing_key.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


def sign_pipedream_webhook_headers(
    *,
    signing_key: str,
    raw_body: bytes,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Build Pipedream delivery headers for one serialized payload."""

    webhook_timestamp = timestamp or str(int(time.time()))
    signed_payload = f"{webhook_timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(
        signing_key.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return {"x-pd-signature": f"t={webhook_timestamp},v1={digest}"}
