"""Shared helpers for signed Composio webhook delivery in provider-trigger tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from httpx import AsyncClient, Response

from orchestra.provider_triggers.composio_trigger_adapter import (
    sign_composio_webhook_headers,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "provider_trigger_contract"
    / "composio_github_issue_created.redacted.json"
)


def load_composio_github_issue_fixture(
    *,
    external_trigger_id: str | None = None,
    connected_account_id: str | None = None,
    provider_user_id: str | None = None,
    repository: str | None = None,
    title: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Load the Composio GitHub issue-created fixture with overrides."""

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    metadata = dict(payload.get("metadata") or {})
    if external_trigger_id is not None:
        metadata["trigger_id"] = external_trigger_id
    if connected_account_id is not None:
        metadata["connected_account_id"] = connected_account_id
    if provider_user_id is not None:
        metadata["user_id"] = provider_user_id
    payload["metadata"] = metadata
    if event_id is not None:
        payload["id"] = event_id
    data = dict(payload.get("data") or {})
    if title is not None:
        data["title"] = title
    if repository is not None:
        repo = dict(data.get("repository") or {})
        repo["full_name"] = repository
        data["repository"] = repo
    payload["data"] = data
    return payload


def serialize_composio_payload(payload: Mapping[str, Any]) -> bytes:
    """Serialize one Composio webhook payload using ingress-stable JSON."""

    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign_composio_payload(
    raw_body: bytes,
    *,
    signing_secret: str,
    webhook_id: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Return Composio delivery headers for one serialized payload."""

    return sign_composio_webhook_headers(
        signing_secret=signing_secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        timestamp=timestamp,
    )


async def deliver_signed_composio_webhook(
    client: AsyncClient,
    *,
    ingress_key: str,
    payload: Mapping[str, Any],
    signing_secret: str,
    webhook_id: str,
    backend_id: str = "composio",
) -> Response:
    """POST one signed Composio-shaped webhook to Orchestra ingress."""

    raw_body = serialize_composio_payload(payload)
    return await client.post(
        f"/v0/webhooks/integrations/{backend_id}/{ingress_key}",
        content=raw_body,
        headers=sign_composio_payload(
            raw_body,
            signing_secret=signing_secret,
            webhook_id=webhook_id,
        ),
    )
