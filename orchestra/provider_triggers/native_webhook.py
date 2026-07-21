"""Shared HMAC verification for native Google/Microsoft trigger deliveries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping, Sequence

from orchestra.provider_triggers.backend_ids import DEFAULT_SIGNATURE_TOLERANCE_SECONDS


def verify_native_webhook_signature(
    *,
    signing_secret: str,
    raw_body: bytes | str,
    webhook_id: str,
    webhook_timestamp: str,
    signature_header: str,
    tolerance_seconds: int = DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
    now_seconds: int | None = None,
) -> bool:
    """Validate native trigger callback signatures."""

    if not signing_secret or not webhook_id or not webhook_timestamp:
        return False
    if not signature_header:
        return False
    try:
        timestamp = int(webhook_timestamp)
    except ValueError:
        return False
    current = int(time.time() if now_seconds is None else now_seconds)
    if tolerance_seconds > 0 and abs(current - timestamp) > tolerance_seconds:
        return False

    body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
    signed_payload = f"{webhook_id}.{webhook_timestamp}.{body}"
    expected = base64.b64encode(
        hmac.new(
            signing_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")

    provided: list[str] = []
    for part in signature_header.split(" "):
        part = part.strip()
        if part.startswith("v1,"):
            provided.append(part[3:])
        elif "," in part:
            _, _, value = part.partition(",")
            if value:
                provided.append(value)
        elif part:
            provided.append(part)
    if not provided:
        return False
    return any(hmac.compare_digest(candidate, expected) for candidate in provided)


def sign_native_webhook_headers(
    *,
    signing_secret: str,
    raw_body: bytes,
    webhook_id: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Build native trigger delivery headers for one body."""

    webhook_timestamp = timestamp or str(int(time.time()))
    body_text = raw_body.decode("utf-8")
    digest = base64.b64encode(
        hmac.new(
            signing_secret.encode("utf-8"),
            f"{webhook_id}.{webhook_timestamp}.{body_text}".encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")
    return {
        "x-unify-webhook-id": webhook_id,
        "x-unify-webhook-timestamp": webhook_timestamp,
        "x-unify-webhook-signature": f"v1,{digest}",
    }


def verify_native_delivery(
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    signing_secrets: Sequence[str],
    tolerance_seconds: int | None = None,
) -> bool:
    """Return True when any accepted secret validates the delivery."""

    webhook_id = _header_value(headers, "x-unify-webhook-id")
    webhook_timestamp = _header_value(headers, "x-unify-webhook-timestamp")
    signature_header = _header_value(headers, "x-unify-webhook-signature")
    tolerance = (
        DEFAULT_SIGNATURE_TOLERANCE_SECONDS
        if tolerance_seconds is None
        else tolerance_seconds
    )
    for secret in signing_secrets:
        if not secret:
            continue
        if verify_native_webhook_signature(
            signing_secret=secret,
            raw_body=raw_body,
            webhook_id=webhook_id,
            webhook_timestamp=webhook_timestamp,
            signature_header=signature_header,
            tolerance_seconds=tolerance,
        ):
            return True
    return False


def _header_value(headers: Mapping[str, str], name: str) -> str:
    direct = headers.get(name)
    if isinstance(direct, str) and direct:
        return direct
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered and isinstance(value, str):
            return value
    return ""
