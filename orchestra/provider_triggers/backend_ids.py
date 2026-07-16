"""Stable provider trigger backend identifiers."""

from __future__ import annotations

COMPOSIO_BACKEND_ID = "composio"
PIPEDREAM_BACKEND_ID = "pipedream"
LOCAL_BACKEND_ID = "local"

COMPOSIO_SIGNATURE_HEADERS = (
    "webhook-id",
    "webhook-timestamp",
    "webhook-signature",
)
COMPOSIO_SIGNATURE_SCHEME = "composio_v3_hmac_sha256"
PIPEDREAM_SIGNATURE_HEADERS = ("x-pd-signature",)
PIPEDREAM_SIGNATURE_SCHEME = "pipedream_hmac_sha256_timestamp_dot_body"
DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300
