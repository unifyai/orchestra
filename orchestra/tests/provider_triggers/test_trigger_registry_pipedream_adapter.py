"""Pipedream trigger adapter passthrough contracts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

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
from orchestra.provider_triggers.trigger_adapter import TriggerProvisionRequest

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "provider_trigger_contract"
)


def _load_pipedream_fixture() -> dict[str, Any]:
    payload = json.loads(
        (FIXTURE_DIR / "pipedream_github_issue.redacted.json").read_text(
            encoding="utf-8",
        ),
    )
    payload.setdefault("component_key", "github-new-or-updated-issue")
    return payload


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _provision_request(**overrides: Any) -> TriggerProvisionRequest:
    payload: dict[str, Any] = {
        "connection_id": "conn-1",
        "provider_connection_id": "pd-account-1",
        "provider_user_id": "assistant:1",
        "canonical_app_slug": "github",
        "provider_trigger_slug": "github-new-or-updated-issue",
        "trigger_config": {"github": {"events": ["opened"]}, "label": "bug"},
        "callback_url": "https://example.test/hooks",
        "idempotency_key": "idem-1",
        "ingress_key": "ingress-1",
    }
    payload.update(overrides)
    return TriggerProvisionRequest(**payload)


def test_pipedream_retry_deliveries_normalize_to_same_identity() -> None:
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
    assert first.provider_trigger_slug == "github-new-or-updated-issue"
    assert first.source_body == second.source_body
    assert "event_slug" not in first.envelope
    assert adapter.stable_event_identity(payload) == first.provider_event_identity


def test_pipedream_provision_posts_passthrough_payload() -> None:
    requests_made: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        requests_made.append((method, url, kwargs))
        if url.endswith("/oauth/token"):
            return _FakeResponse(payload={"access_token": "token-123"})
        if url.endswith("/triggers/deploy"):
            return _FakeResponse(
                payload={
                    "data": {
                        "id": "dc_created_1",
                        "webhook_signing_key": "signing-secret-1",
                    },
                },
            )
        return _FakeResponse(status_code=500, text="unexpected")

    adapter = PipedreamTriggerAdapter(
        client_id="test",
        client_secret="test",
        project_id="proj_test",
        oauth_token_url="https://api.pipedream.com/v1/oauth/token",
        request_fn=request_fn,
    )
    request = _provision_request(idempotency_key="idem-42")

    result = adapter.provision(request)

    assert result.external_trigger_id == "dc_created_1"
    assert result.signing_secret_ref is not None
    deploy_call = next(
        item for item in requests_made if item[1].endswith("/triggers/deploy")
    )
    _, _, kwargs = deploy_call
    assert kwargs["json"] == {
        "external_user_id": "assistant:1",
        "id": "github-new-or-updated-issue",
        "configured_props": {
            "github": {
                "events": ["opened"],
                "authProvisionId": "pd-account-1",
            },
            "label": "bug",
        },
        "webhook_url": "https://example.test/hooks",
        "emit_on_deploy": False,
    }


def test_local_pipedream_stub_provision_is_deterministic_for_slug_and_config() -> None:
    adapter = LocalPipedreamTriggerAdapter()
    first = adapter.provision(
        _provision_request(
            idempotency_key="idem-1",
            ingress_key="ingress-1",
            generation_id="gen-1",
        ),
    )
    second = adapter.provision(
        _provision_request(
            idempotency_key="idem-2",
            ingress_key="ingress-2",
            generation_id="gen-2",
        ),
    )

    assert first.external_trigger_id == second.external_trigger_id


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
