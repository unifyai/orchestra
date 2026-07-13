"""Live provider catalog and signing probes for GitHub issue-created triggers."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from orchestra.provider_triggers.provider_identity import redact_mapping


@dataclass
class ProviderProbeResult:
    """Pinned observations from one provider mapping probe."""

    backend_id: str
    canonical_event_slug: str
    provider_trigger_slug: str
    provider_trigger_version: str | None
    retry_stable_identity_field: str
    signature_headers: list[str]
    signature_scheme: str
    timestamp_tolerance_seconds: int | None
    provisioning_idempotency_supported: bool
    catalog_status: str
    notes: list[str] = field(default_factory=list)
    fixture_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "canonical_event_slug": self.canonical_event_slug,
            "provider_trigger_slug": self.provider_trigger_slug,
            "provider_trigger_version": self.provider_trigger_version,
            "retry_stable_identity_field": self.retry_stable_identity_field,
            "signature_headers": self.signature_headers,
            "signature_scheme": self.signature_scheme,
            "timestamp_tolerance_seconds": self.timestamp_tolerance_seconds,
            "provisioning_idempotency_supported": self.provisioning_idempotency_supported,
            "catalog_status": self.catalog_status,
            "notes": self.notes,
            "fixture_payload": self.fixture_payload,
        }


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for live provider probes")
    return value


def probe_composio_github_issue_created() -> ProviderProbeResult:
    """Discover the Composio GitHub issue-created mapping and signing contract."""

    from composio import Composio

    api_key = _require_env("COMPOSIO_API_KEY")
    composio = Composio(api_key=api_key)
    trigger_type = composio.triggers.get_type("GITHUB_ISSUE_CREATED_TRIGGER")
    trigger_dump = (
        trigger_type.model_dump()
        if hasattr(trigger_type, "model_dump")
        else dict(trigger_type)
    )
    fixture = {
        "id": "evt_provider_trigger_example",
        "type": "composio.trigger.message",
        "metadata": {
            "trigger_slug": "GITHUB_ISSUE_CREATED_TRIGGER",
            "trigger_id": "ti_provider_trigger_example",
            "connected_account_id": "ca_provider_trigger_example",
            "user_id": "assistant:provider-trigger-probe",
        },
        "data": {
            "action": "opened",
            "issue_number": 42,
            "title": "Provider trigger fixture",
            "repository": {"full_name": "octocat/Hello-World"},
            "user": {"login": "octocat"},
            "labels": [{"name": "bug"}],
        },
        "timestamp": "2026-01-15T10:30:00Z",
    }
    return ProviderProbeResult(
        backend_id="composio",
        canonical_event_slug="github.issue_created",
        provider_trigger_slug="GITHUB_ISSUE_CREATED_TRIGGER",
        provider_trigger_version=str(
            trigger_dump.get("version") or trigger_dump.get("toolkit_version"),
        ),
        retry_stable_identity_field="id",
        signature_headers=["webhook-id", "webhook-timestamp", "webhook-signature"],
        signature_scheme="composio_v3_hmac_sha256",
        timestamp_tolerance_seconds=300,
        provisioning_idempotency_supported=True,
        catalog_status="included",
        notes=[
            "Composio catalog slug is GITHUB_ISSUE_CREATED_TRIGGER; Orchestra registry maps it to github.issue_created.",
            "V3 deliveries expose a top-level id used as the retry-stable provider event identity.",
            "Create/delete and lost-response recovery require a disposable connected GitHub account.",
        ],
        fixture_payload=redact_mapping(fixture),
    )


def _pipedream_access_token() -> str:
    client_id = _require_env("PIPEDREAM_CLIENT_ID")
    client_secret = _require_env("PIPEDREAM_CLIENT_SECRET")
    response = requests.post(
        "https://api.pipedream.com/v1/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def probe_pipedream_github_issue_created() -> ProviderProbeResult:
    """Discover the closest Pipedream GitHub issue-created mapping."""

    project_id = _require_env("PIPEDREAM_PROJECT_ID")
    environment = os.getenv("PIPEDREAM_ENVIRONMENT", "development").strip()
    token = _pipedream_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "x-pd-environment": environment,
    }
    component_key = "github-new-or-updated-issue"
    response = requests.get(
        f"https://api.pipedream.com/v1/connect/{project_id}/components/{component_key}",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    component = response.json().get("data") or response.json()
    fixture = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Provider trigger fixture",
            "labels": [{"name": "bug"}],
            "user": {"login": "octocat"},
        },
        "repository": {"full_name": "octocat/Hello-World"},
        "trace_id": "pd_trace_provider_trigger_example",
    }
    return ProviderProbeResult(
        backend_id="pipedream",
        canonical_event_slug="github.issue_created",
        provider_trigger_slug=component_key,
        provider_trigger_version=str(component.get("version")),
        retry_stable_identity_field="trace_id",
        signature_headers=["x-pd-signature"],
        signature_scheme="pipedream_hmac_sha256_timestamp_dot_body",
        timestamp_tolerance_seconds=300,
        provisioning_idempotency_supported=True,
        catalog_status="included_with_opened_filter",
        notes=[
            "Pipedream exposes github-new-or-updated-issue rather than issue-created-only.",
            "Canonical github.issue_created projection must treat action=opened as create events.",
            "Per-generation webhook_signing_key is returned only at deploy/update time.",
        ],
        fixture_payload=redact_mapping(fixture),
    )


def verify_pipedream_signature(
    *,
    signing_key: str,
    raw_body: bytes,
    signature_header: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Validate an x-pd-signature header against the documented scheme."""

    parts: dict[str, str] = {}
    for segment in signature_header.split(","):
        key, _, value = segment.partition("=")
        parts[key.strip()] = value.strip()
    timestamp = parts.get("t")
    provided = parts.get("v1")
    if not timestamp or not provided:
        return False
    if abs(int(time.time()) - int(timestamp)) > tolerance_seconds:
        return False
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(
        signing_key.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


def provider_probes_available() -> bool:
    """Return True when staging provider secrets are available locally."""

    required = (
        "COMPOSIO_API_KEY",
        "PIPEDREAM_CLIENT_ID",
        "PIPEDREAM_CLIENT_SECRET",
        "PIPEDREAM_PROJECT_ID",
    )
    return all(os.getenv(name, "").strip() for name in required)
