"""Shared helpers for signed Pipedream webhook delivery in provider-trigger tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from httpx import AsyncClient, Response

from orchestra.provider_triggers.pipedream_signing import sign_pipedream_webhook_headers

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "provider_trigger_contract"
    / "pipedream_github_issue.redacted.json"
)


def load_pipedream_github_issue_fixture(
    *,
    repository: str | None = None,
    title: str | None = None,
    action: str = "opened",
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Load the curated Pipedream GitHub issue fixture with overrides."""

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["action"] = action
    if trace_id is not None:
        payload["trace_id"] = trace_id
    if title is not None:
        issue = dict(payload.get("issue") or {})
        issue["title"] = title
        payload["issue"] = issue
    if repository is not None:
        repo = dict(payload.get("repository") or {})
        repo["full_name"] = repository
        payload["repository"] = repo
    return payload


def serialize_pipedream_payload(payload: Mapping[str, Any]) -> bytes:
    """Serialize one Pipedream webhook payload using ingress-stable JSON."""

    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign_pipedream_payload(
    raw_body: bytes,
    *,
    signing_secret: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Return Pipedream delivery headers for one serialized payload."""

    return sign_pipedream_webhook_headers(
        signing_key=signing_secret,
        raw_body=raw_body,
        timestamp=timestamp,
    )


async def deliver_signed_pipedream_webhook(
    client: AsyncClient,
    *,
    ingress_key: str,
    payload: Mapping[str, Any],
    signing_secret: str,
    backend_id: str = "pipedream",
) -> Response:
    """POST one signed Pipedream-shaped webhook to Orchestra ingress."""

    raw_body = serialize_pipedream_payload(payload)
    return await client.post(
        f"/v0/webhooks/integrations/{backend_id}/{ingress_key}",
        content=raw_body,
        headers=sign_pipedream_payload(raw_body, signing_secret=signing_secret),
    )
