"""Shared helpers for signed native trigger webhook delivery in tests."""

from __future__ import annotations

import json
from typing import Any, Mapping

from httpx import AsyncClient, Response

from orchestra.provider_triggers.backend_ids import NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG
from orchestra.provider_triggers.native_webhook import sign_native_webhook_headers


def build_native_meet_transcript_payload(
    *,
    event_id: str,
    external_trigger_id: str | None = None,
    connected_account_id: str | None = None,
    provider_user_id: str | None = None,
    transcript_file_id: str = "drive-file-123",
    meeting_code: str = "abc-defg-hij",
) -> dict[str, Any]:
    """Build a native Google Meet transcript-ready delivery payload."""

    return {
        "event_id": event_id,
        "provider_trigger_slug": NATIVE_GOOGLE_MEET_TRANSCRIPT_SLUG,
        "external_trigger_id": external_trigger_id,
        "connected_account_id": connected_account_id,
        "provider_user_id": provider_user_id,
        "occurred_at": "2026-07-20T12:00:00Z",
        "data": {
            "event_id": event_id,
            "transcript_file_id": transcript_file_id,
            "meeting_code": meeting_code,
            "organizer_email": provider_user_id,
        },
    }


def serialize_native_payload(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign_native_payload(
    raw_body: bytes,
    *,
    signing_secret: str,
    webhook_id: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    return sign_native_webhook_headers(
        signing_secret=signing_secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        timestamp=timestamp,
    )


async def deliver_signed_native_webhook(
    client: AsyncClient,
    *,
    backend_id: str,
    ingress_key: str,
    payload: Mapping[str, Any],
    signing_secret: str,
    webhook_id: str,
) -> Response:
    raw_body = serialize_native_payload(payload)
    headers = sign_native_payload(
        raw_body,
        signing_secret=signing_secret,
        webhook_id=webhook_id,
    )
    return await client.post(
        f"/v0/webhooks/integrations/{backend_id}/{ingress_key}",
        content=raw_body,
        headers=headers,
    )
