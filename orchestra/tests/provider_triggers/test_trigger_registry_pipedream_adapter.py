"""Curated registry and Pipedream trigger adapter contracts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from orchestra.provider_triggers.local_pipedream_trigger_adapter import (
    LocalPipedreamTriggerAdapter,
)
from orchestra.provider_triggers.pipedream_signing import (
    sign_pipedream_webhook_headers,
    verify_pipedream_signature,
)
from orchestra.provider_triggers.pipedream_trigger_adapter import (
    PipedreamTriggerAdapter,
)
from orchestra.provider_triggers.trigger_adapter_registry import (
    get_trigger_provider_adapter,
)
from orchestra.provider_triggers.trigger_matching import project_github_issue_created
from orchestra.provider_triggers.trigger_registry import (
    GITHUB_ISSUE_CREATED,
    PIPEDREAM_BACKEND_ID,
    list_trigger_catalog_payloads,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "provider_trigger_contract"
)


def _load_pipedream_fixture() -> dict[str, Any]:
    return json.loads(
        (FIXTURE_DIR / "pipedream_github_issue.redacted.json").read_text(
            encoding="utf-8",
        ),
    )


def test_pipedream_retry_deliveries_normalize_to_same_identity_and_projection() -> None:
    payload = _load_pipedream_fixture()
    adapter = PipedreamTriggerAdapter(
        client_id="test",
        client_secret="test",
        project_id="proj_test",
    )
    first = adapter.normalize_delivery(headers={}, raw_body=payload)
    second = adapter.normalize_delivery(
        headers={},
        raw_body=json.dumps(payload).encode("utf-8"),
    )

    assert first.provider_event_identity == "pd_trace_provider_trigger_example"
    assert second.provider_event_identity == first.provider_event_identity
    assert first.curated_projection == second.curated_projection
    assert first.provider_trigger_slug == "github-new-or-updated-issue"
    assert first.envelope["event_slug"] == GITHUB_ISSUE_CREATED
    assert adapter.stable_event_identity(payload) == first.provider_event_identity


def test_pipedream_non_opened_action_is_rejected_at_normalize() -> None:
    payload = _load_pipedream_fixture()
    payload["action"] = "closed"
    adapter = PipedreamTriggerAdapter(
        client_id="test",
        client_secret="test",
        project_id="proj_test",
    )
    with pytest.raises(ValueError, match="Unsupported Pipedream action"):
        adapter.normalize_delivery(headers={}, raw_body=payload)


def test_pipedream_signature_vector_matches_fixture_scheme() -> None:
    payload = _load_pipedream_fixture()
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    headers = sign_pipedream_webhook_headers(
        signing_key="pd_test_signing_key",
        raw_body=raw_body,
        timestamp=timestamp,
    )
    assert verify_pipedream_signature(
        signing_key="pd_test_signing_key",
        raw_body=raw_body,
        signature_header=headers["x-pd-signature"],
        tolerance_seconds=300,
        now_seconds=int(timestamp),
    )


def test_pipedream_fixture_projects_through_github_issue_created_helper() -> None:
    payload = _load_pipedream_fixture()
    projection = project_github_issue_created(payload)
    assert projection == {
        "repository": "octocat/hello-world",
        "author": "octocat",
        "labels": ["bug"],
        "title": "provider trigger fixture",
    }


def test_registry_returns_local_pipedream_stub_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIPEDREAM_CLIENT_ID", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PIPEDREAM_PROJECT_ID", raising=False)
    adapter = get_trigger_provider_adapter("pipedream")
    assert isinstance(adapter, LocalPipedreamTriggerAdapter)


def test_catalog_advertises_pipedream_backend_for_github_issue_created() -> None:
    events = list_trigger_catalog_payloads()
    github_event = next(
        event for event in events if event["event_slug"] == GITHUB_ISSUE_CREATED
    )
    assert PIPEDREAM_BACKEND_ID in github_event["backends"]
