"""Composio trigger adapter passthrough contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import pytest

from orchestra.provider_triggers.composio_trigger_adapter import (
    ComposioTriggerAdapter,
    list_composio_trigger_types,
    verify_composio_signature,
)
from orchestra.provider_triggers.local_composio_trigger_adapter import (
    LocalComposioTriggerAdapter,
)
from orchestra.provider_triggers.trigger_adapter import (
    TriggerDeleteRequest,
    TriggerProvisionRequest,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "provider_trigger_contract"
)


def _load_composio_fixture() -> dict[str, Any]:
    return json.loads(
        (FIXTURE_DIR / "composio_github_issue_created.redacted.json").read_text(
            encoding="utf-8",
        ),
    )


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
        "provider_connection_id": "ca_active",
        "provider_user_id": "assistant:1",
        "canonical_app_slug": "github",
        "provider_trigger_slug": "GITHUB_ISSUE_CREATED_TRIGGER",
        "trigger_config": {"repository": "octocat/hello-world"},
        "callback_url": "https://example.test/hooks",
        "idempotency_key": "idem-1",
        "ingress_key": "ingress-1",
    }
    payload.update(overrides)
    return TriggerProvisionRequest(**payload)


def test_composio_retry_deliveries_normalize_to_same_identity() -> None:
    payload = _load_composio_fixture()
    adapter = ComposioTriggerAdapter(api_key="test-key", webhook_secret="secret")
    first = adapter.normalize_delivery(
        headers={"webhook-id": "msg_retry_1"},
        raw_body=payload,
    )
    second = adapter.normalize_delivery(
        headers={"webhook-id": "msg_retry_2"},
        raw_body=json.dumps(payload).encode("utf-8"),
    )

    assert first.provider_event_identity == "evt_provider_trigger_example"
    assert second.provider_event_identity == first.provider_event_identity
    assert first.provider_trigger_slug == "GITHUB_ISSUE_CREATED_TRIGGER"
    assert first.source_body == second.source_body
    assert "event_slug" not in first.envelope
    assert first.envelope["provider_trigger_slug"] == first.provider_trigger_slug
    assert adapter.stable_event_identity(payload) == first.provider_event_identity


def test_composio_provision_posts_upsert_with_passthrough_payload() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url, kwargs))
        return _FakeResponse(payload={"trigger_id": "ti_created_1"})

    adapter = ComposioTriggerAdapter(
        api_key="test-key",
        base_url="https://backend.composio.dev/api/v3.1",
        request_fn=request_fn,
    )
    request = _provision_request(
        trigger_config={"repository": "octocat/hello-world", "labels": ["bug"]},
        idempotency_key="idem-42",
    )

    result = adapter.provision(request)

    assert result.external_trigger_id == "ti_created_1"
    assert result.signing_secret_ref == "env:COMPOSIO_WEBHOOK_SECRET"
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url.endswith(
        "/trigger_instances/GITHUB_ISSUE_CREATED_TRIGGER/upsert",
    )
    assert kwargs["headers"]["x-api-key"] == "test-key"
    assert kwargs["json"] == {
        "connected_account_id": "ca_active",
        "user_id": "assistant:1",
        "trigger_config": {"repository": "octocat/hello-world", "labels": ["bug"]},
    }


@pytest.mark.parametrize("status_code", [200, 404, 410])
def test_composio_delete_treats_missing_trigger_as_success(status_code: int) -> None:
    calls: list[tuple[str, str]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        del kwargs
        calls.append((method, url))
        return _FakeResponse(status_code=status_code)

    adapter = ComposioTriggerAdapter(
        api_key="test-key",
        base_url="https://backend.composio.dev/api/v3.1",
        request_fn=request_fn,
    )
    adapter.delete(
        TriggerDeleteRequest(
            external_trigger_id="ti_to_delete",
            idempotency_key="idem-delete-1",
        ),
    )

    assert calls == [
        (
            "DELETE",
            "https://backend.composio.dev/api/v3.1/trigger_instances/manage/ti_to_delete",
        ),
    ]


def test_composio_delete_raises_on_server_error() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        del method, url, kwargs
        return _FakeResponse(status_code=500, text="boom")

    adapter = ComposioTriggerAdapter(api_key="test-key", request_fn=request_fn)
    with pytest.raises(RuntimeError, match="Composio trigger delete failed"):
        adapter.delete(
            TriggerDeleteRequest(
                external_trigger_id="ti_to_delete",
                idempotency_key="idem-delete-2",
            ),
        )


def test_list_composio_trigger_types_paginates_with_cursor() -> None:
    calls: list[dict[str, Any]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        assert method == "GET"
        assert url.endswith("/triggers_types")
        calls.append(kwargs)
        params = kwargs.get("params") or {}
        if params.get("cursor") == "page-2":
            return _FakeResponse(
                payload={
                    "items": [
                        {
                            "slug": "GMAIL_NEW_GMAIL_MESSAGE",
                            "version": "2",
                            "toolkit": {"slug": "gmail"},
                            "config": {"type": "object"},
                        },
                    ],
                    "next_cursor": None,
                },
            )
        return _FakeResponse(
            payload={
                "items": [
                    {
                        "slug": "GITHUB_ISSUE_CREATED_TRIGGER",
                        "version": "1",
                        "toolkit": {"slug": "github"},
                        "config": {"type": "object"},
                    },
                ],
                "next_cursor": "page-2",
            },
        )

    entries = list_composio_trigger_types(
        api_key="test-key",
        base_url="https://backend.composio.dev/api/v3.1",
        request_fn=request_fn,
        page_limit=250,
    )

    assert [entry["slug"] for entry in entries] == [
        "GITHUB_ISSUE_CREATED_TRIGGER",
        "GMAIL_NEW_GMAIL_MESSAGE",
    ]
    assert calls[0]["headers"]["x-api-key"] == "test-key"
    assert calls[0]["params"] == {"limit": 250}
    assert calls[1]["params"] == {"limit": 250, "cursor": "page-2"}


def test_local_composio_stub_provision_is_deterministic_for_slug_and_config() -> None:
    adapter = LocalComposioTriggerAdapter(webhook_secret="secret")
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


def test_verify_composio_signature_accepts_v1_hmac_over_id_timestamp_body() -> None:
    raw_body = b'{"id":"evt_1"}'
    webhook_id = "msg_1"
    timestamp = str(int(time.time()))
    secret = "composio-webhook-secret"
    digest = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            f"{webhook_id}.{timestamp}.{raw_body.decode()}".encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")
    assert verify_composio_signature(
        signing_secret=secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        webhook_timestamp=timestamp,
        signature_header=f"v1,{digest}",
    )
    assert not verify_composio_signature(
        signing_secret=secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        webhook_timestamp=timestamp,
        signature_header="v1,deadbeef",
    )
    assert not verify_composio_signature(
        signing_secret=secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        webhook_timestamp=str(int(time.time()) - 10_000),
        signature_header=f"v1,{digest}",
    )

    adapter = ComposioTriggerAdapter(api_key="test-key", webhook_secret=secret)
    assert adapter.verify_delivery(
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{digest}",
        },
        raw_body=raw_body,
        signing_secrets=[],
    )
