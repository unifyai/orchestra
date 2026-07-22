"""Native Microsoft Graph trigger adapter contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orchestra.provider_triggers.native_microsoft_trigger_adapter import (
    NativeMicrosoftTriggerAdapter,
)
from orchestra.provider_triggers.trigger_adapter import (
    TriggerDeleteRequest,
    TriggerProvisionRequest,
)
from orchestra.provider_triggers.workspace_trigger_credentials import (
    WorkspaceTriggerCredentials,
)

MAIL_SLUG = "microsoft.graph.mailMessage.created"
MEETING_SLUG = "microsoft.graph.callTranscript.created.meeting"
TODO_SLUG = "microsoft.graph.todoTask.created"
APP_ONLY_SLUG = "microsoft.graph.user.updated"
SUBSCRIPTION_ID = "7f105c7d-2dc5-4530-97cd-4e7ae6534c07"
ADAPTERS_URL = "https://adapters.example.test"
NOTIFICATION_URL = f"{ADAPTERS_URL}/microsoft/native-triggers"
WEBHOOK_SECRET = "native-microsoft-secret"


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


class _FakeCredentialLoader:
    def __init__(self, credentials: WorkspaceTriggerCredentials | None) -> None:
        self._credentials = credentials
        self.calls: list[str] = []

    def load_for_connection_id(
        self,
        connection_id: str,
    ) -> WorkspaceTriggerCredentials:
        self.calls.append(connection_id)
        if self._credentials is None:
            raise LookupError(connection_id)
        return self._credentials


def _credentials(
    *,
    access_token: str = "ms-test-access-token",
) -> WorkspaceTriggerCredentials:
    return WorkspaceTriggerCredentials(
        connection_id="ic_ws_native_microsoft_microsoft_outlook_1",
        provider_connection_id="microsoft:outlook.user@example.com",
        account_email="outlook.user@example.com",
        access_token=access_token,
        refresh_token="refresh",
        granted_scopes=("Mail.Read",),
        secret_values={},
    )


def _provision_request(**overrides: Any) -> TriggerProvisionRequest:
    payload: dict[str, Any] = {
        "connection_id": "ic_ws_native_microsoft_microsoft_outlook_1",
        "provider_connection_id": "microsoft:outlook.user@example.com",
        "provider_user_id": "outlook.user@example.com",
        "canonical_app_slug": "microsoft_outlook",
        "provider_trigger_slug": MAIL_SLUG,
        "trigger_config": {},
        "callback_url": (
            "https://triggers.example.test/v0/webhooks/integrations/"
            "native_microsoft/ingress-1"
        ),
        "idempotency_key": "idem-1",
        "ingress_key": "ingress-1",
    }
    payload.update(overrides)
    return TriggerProvisionRequest(**payload)


def _adapter(
    *,
    request_fn: Any,
    credentials: WorkspaceTriggerCredentials | None = None,
    adapters_base_url: str | None = ADAPTERS_URL,
    webhook_secret: str | None = WEBHOOK_SECRET,
) -> NativeMicrosoftTriggerAdapter:
    loader = _FakeCredentialLoader(
        _credentials() if credentials is None else credentials,
    )
    return NativeMicrosoftTriggerAdapter(
        credential_loader=loader,  # type: ignore[arg-type]
        webhook_secret=webhook_secret,
        adapters_base_url=adapters_base_url,
        request_fn=request_fn,
    )


def test_native_microsoft_provision_creates_graph_subscription_for_mail() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/subscriptions"):
            return _FakeResponse(
                payload={
                    "id": SUBSCRIPTION_ID,
                    "resource": "me/messages",
                    "changeType": "created",
                    "expirationDateTime": "2026-07-24T12:00:00.0000000Z",
                },
            )
        raise AssertionError(f"unexpected request {method} {url}")

    adapter = _adapter(request_fn=request_fn)
    result = adapter.provision(_provision_request())

    assert result.external_trigger_id == SUBSCRIPTION_ID
    assert result.signing_secret_ref == "env:NATIVE_MICROSOFT_WEBHOOK_SECRET"
    assert not result.external_trigger_id.startswith("nm_")

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://graph.microsoft.com/v1.0/subscriptions"
    body = kwargs["json"]
    assert body["resource"] == "me/messages"
    assert body["changeType"] == "created"
    assert body["notificationUrl"] == NOTIFICATION_URL
    assert body["lifecycleNotificationUrl"] == NOTIFICATION_URL
    assert body["clientState"] == WEBHOOK_SECRET
    assert "expirationDateTime" in body


@pytest.mark.parametrize(
    ("slug", "app_slug", "trigger_config", "expected_resource"),
    [
        (
            MEETING_SLUG,
            "microsoft_teams",
            {"online_meeting_id": "meeting-abc"},
            "communications/onlineMeetings/meeting-abc/transcripts",
        ),
        (
            TODO_SLUG,
            "microsoft_todo",
            {"todo_task_list_id": "list-123"},
            "me/todo/lists/list-123/tasks",
        ),
    ],
)
def test_native_microsoft_provision_substitutes_resource_templates(
    slug: str,
    app_slug: str,
    trigger_config: dict[str, Any],
    expected_resource: str,
) -> None:
    calls: list[dict[str, Any]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append(kwargs)
        return _FakeResponse(payload={"id": SUBSCRIPTION_ID})

    adapter = _adapter(request_fn=request_fn)
    result = adapter.provision(
        _provision_request(
            provider_trigger_slug=slug,
            canonical_app_slug=app_slug,
            trigger_config=trigger_config,
        ),
    )
    assert result.external_trigger_id == SUBSCRIPTION_ID
    assert calls[0]["json"]["resource"] == expected_resource


@pytest.mark.parametrize(
    ("slug", "trigger_config", "match"),
    [
        (APP_ONLY_SLUG, {}, "app-only"),
        (TODO_SLUG, {}, "todo_task_list_id"),
    ],
)
def test_native_microsoft_provision_fails_closed_for_app_only_or_missing_config(
    slug: str,
    trigger_config: dict[str, Any],
    match: str,
) -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected for fail-closed provision")

    adapter = _adapter(request_fn=request_fn)
    with pytest.raises(RuntimeError, match=match):
        adapter.provision(
            _provision_request(
                provider_trigger_slug=slug,
                trigger_config=trigger_config,
            ),
        )


def test_native_microsoft_provision_fails_closed_without_adapters_url() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected without adapters URL")

    adapter = _adapter(request_fn=request_fn, adapters_base_url="")
    with pytest.raises(RuntimeError, match="UNITY_ADAPTERS_URL"):
        adapter.provision(_provision_request())


@pytest.mark.parametrize("status_code", [200, 404, 410])
def test_native_microsoft_delete_removes_subscription_and_tolerates_missing(
    status_code: int,
) -> None:
    calls: list[tuple[str, str]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url))
        return _FakeResponse(status_code=status_code)

    adapter = _adapter(request_fn=request_fn)
    adapter.delete(
        TriggerDeleteRequest(
            external_trigger_id=SUBSCRIPTION_ID,
            idempotency_key="idem-delete-1",
            connection_id="ic_ws_native_microsoft_microsoft_outlook_1",
        ),
    )
    assert calls == [
        ("DELETE", f"https://graph.microsoft.com/v1.0/subscriptions/{SUBSCRIPTION_ID}"),
    ]


def test_native_microsoft_delete_skips_legacy_stub_id() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected for a legacy stub id")

    adapter = _adapter(request_fn=request_fn)
    adapter.delete(
        TriggerDeleteRequest(
            external_trigger_id="nm_deadbeef1234",
            idempotency_key="idem-delete-3",
            connection_id="ic_ws_native_microsoft_microsoft_outlook_1",
        ),
    )


def test_native_microsoft_health_renews_near_expiry() -> None:
    near = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime(
        "%Y-%m-%dT%H:%M:%S.0000000Z",
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url, kwargs))
        if method == "GET":
            return _FakeResponse(
                payload={"id": SUBSCRIPTION_ID, "expirationDateTime": near},
            )
        if method == "PATCH":
            return _FakeResponse(
                payload={"id": SUBSCRIPTION_ID, "expirationDateTime": near},
            )
        raise AssertionError(f"unexpected request {method} {url}")

    adapter = _adapter(request_fn=request_fn)
    result = adapter.health(
        external_trigger_id=SUBSCRIPTION_ID,
        provider_connection_id="microsoft:outlook.user@example.com",
        connection_id="ic_ws_native_microsoft_microsoft_outlook_1",
    )
    assert result.status == "ok"
    assert result.detail["renewed"] is True
    assert any(method == "PATCH" for method, _url, _kwargs in calls)


@pytest.mark.parametrize(
    ("status_code", "payload", "error_code"),
    [
        (404, {}, "provider_subscription_missing"),
        (410, {}, "provider_subscription_missing"),
        (
            200,
            {
                "id": SUBSCRIPTION_ID,
                "expirationDateTime": "2020-01-01T00:00:00.0000000Z",
            },
            "provider_subscription_missing",
        ),
    ],
)
def test_native_microsoft_health_maps_dead_states(
    status_code: int,
    payload: dict[str, Any],
    error_code: str,
) -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(status_code=status_code, payload=payload)

    adapter = _adapter(request_fn=request_fn)
    result = adapter.health(
        external_trigger_id=SUBSCRIPTION_ID,
        provider_connection_id="microsoft:outlook.user@example.com",
        connection_id="ic_ws_native_microsoft_microsoft_outlook_1",
    )
    assert result.status == "error"
    assert result.error_code == error_code


def test_native_microsoft_health_rejects_stub_external_id() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected for stub ids")

    adapter = _adapter(request_fn=request_fn)
    result = adapter.health(
        external_trigger_id="nm_stub_or_future_graph_id",
        provider_connection_id="microsoft:outlook.user@example.com",
        connection_id="ic_ws_native_microsoft_microsoft_outlook_1",
    )
    assert result.status == "error"
    assert result.error_code == "provider_subscription_missing"
