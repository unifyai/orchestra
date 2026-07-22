"""Native Google Workspace Events trigger adapter contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orchestra.provider_triggers.local_native_google_trigger_adapter import (
    LocalNativeGoogleTriggerAdapter,
)
from orchestra.provider_triggers.native_google_trigger_adapter import (
    NativeGoogleTriggerAdapter,
)
from orchestra.provider_triggers.trigger_adapter import (
    TriggerDeleteRequest,
    TriggerProvisionRequest,
)
from orchestra.provider_triggers.workspace_trigger_credentials import (
    WorkspaceTriggerCredentials,
)

MEET_EVENT_TYPE = "google.workspace.meet.transcript.v2.fileGenerated"
PUBSUB_TOPIC = "projects/test-proj/topics/workspace-events-staging"
SUBSCRIPTION_NAME = "subscriptions/AbCdEf123456"


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
    access_token: str = "ya29.test-access-token",
) -> WorkspaceTriggerCredentials:
    return WorkspaceTriggerCredentials(
        connection_id="ic_ws_native_google_google_meet_1",
        provider_connection_id="google:meet.user@example.com",
        account_email="meet.user@example.com",
        access_token=access_token,
        refresh_token="refresh",
        granted_scopes=("https://www.googleapis.com/auth/meetings.space.readonly",),
        secret_values={},
    )


def _provision_request(**overrides: Any) -> TriggerProvisionRequest:
    payload: dict[str, Any] = {
        "connection_id": "ic_ws_native_google_google_meet_1",
        "provider_connection_id": "google:meet.user@example.com",
        "provider_user_id": "meet.user@example.com",
        "canonical_app_slug": "google_meet",
        "provider_trigger_slug": MEET_EVENT_TYPE,
        "trigger_config": {},
        "callback_url": "https://triggers.example.test/v0/webhooks/integrations/native_google/ingress-1",
        "idempotency_key": "idem-1",
        "ingress_key": "ingress-1",
    }
    payload.update(overrides)
    return TriggerProvisionRequest(**payload)


def _adapter(
    *,
    request_fn: Any,
    credentials: WorkspaceTriggerCredentials | None = None,
    pubsub_topic: str | None = PUBSUB_TOPIC,
) -> NativeGoogleTriggerAdapter:
    loader = _FakeCredentialLoader(
        _credentials() if credentials is None else credentials,
    )
    return NativeGoogleTriggerAdapter(
        credential_loader=loader,  # type: ignore[arg-type]
        webhook_secret="native-google-secret",
        pubsub_topic=pubsub_topic,
        request_fn=request_fn,
    )


def test_native_google_provision_creates_user_level_workspace_events_subscription() -> (
    None
):
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url, kwargs))
        if url.endswith("/userinfo"):
            return _FakeResponse(payload={"sub": "108234567890123456789"})
        if method == "POST" and url.endswith("/subscriptions"):
            return _FakeResponse(
                payload={
                    "done": True,
                    "response": {
                        "@type": "type.googleapis.com/google.apps.events.subscriptions.v1.Subscription",
                        "name": SUBSCRIPTION_NAME,
                        "state": "ACTIVE",
                    },
                },
            )
        raise AssertionError(f"unexpected request {method} {url}")

    adapter = _adapter(request_fn=request_fn)
    result = adapter.provision(_provision_request())

    assert result.external_trigger_id == SUBSCRIPTION_NAME
    assert result.signing_secret_ref == "env:NATIVE_GOOGLE_WEBHOOK_SECRET"
    assert result.signing_secret_version == "project"

    userinfo_call = next(c for c in calls if c[1].endswith("/userinfo"))
    assert userinfo_call[0] == "GET"
    assert (
        userinfo_call[2]["headers"]["Authorization"] == "Bearer ya29.test-access-token"
    )

    create_call = next(c for c in calls if c[1].endswith("/subscriptions"))
    method, url, kwargs = create_call
    assert method == "POST"
    assert url == "https://workspaceevents.googleapis.com/v1/subscriptions"
    assert kwargs["json"] == {
        "targetResource": (
            "//cloudidentity.googleapis.com/users/108234567890123456789"
        ),
        "eventTypes": [MEET_EVENT_TYPE],
        "notificationEndpoint": {"pubsubTopic": PUBSUB_TOPIC},
        "payloadOptions": {"includeResource": False},
    }
    assert not result.external_trigger_id.startswith("ng_")


def test_native_google_provision_fails_closed_without_pubsub_topic() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected when topic is unconfigured")

    adapter = _adapter(request_fn=request_fn, pubsub_topic="")
    with pytest.raises(RuntimeError, match="pubsub topic is not configured"):
        adapter.provision(_provision_request())


def test_native_google_provision_reads_workspace_events_topic_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructor topic=None resolves NATIVE_GOOGLE_WORKSPACE_EVENTS_PUBSUB_TOPIC."""

    from orchestra.settings import settings

    monkeypatch.setattr(
        settings,
        "native_google_workspace_events_pubsub_topic",
        PUBSUB_TOPIC,
    )

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url, kwargs))
        if url.endswith("/userinfo"):
            return _FakeResponse(payload={"sub": "108234567890123456789"})
        if method == "POST" and url.endswith("/subscriptions"):
            return _FakeResponse(
                payload={
                    "done": True,
                    "response": {
                        "@type": (
                            "type.googleapis.com/google.apps.events."
                            "subscriptions.v1.Subscription"
                        ),
                        "name": SUBSCRIPTION_NAME,
                        "state": "ACTIVE",
                    },
                },
            )
        raise AssertionError(f"unexpected request {method} {url}")

    adapter = _adapter(request_fn=request_fn, pubsub_topic=None)
    result = adapter.provision(_provision_request())

    create_call = next(c for c in calls if c[1].endswith("/subscriptions"))
    assert create_call[2]["json"]["notificationEndpoint"]["pubsubTopic"] == PUBSUB_TOPIC
    assert result.external_trigger_id == SUBSCRIPTION_NAME


DRIVE_EVENT_TYPE = "google.workspace.drive.file.v3.created"
CHAT_EVENT_TYPE = "google.workspace.chat.message.v1.created"
CHAT_BATCH_EVENT_TYPE = "google.workspace.chat.message.v1.batchCreated"


def test_native_google_provision_drive_target_with_include_descendants() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/subscriptions"):
            return _FakeResponse(
                payload={
                    "done": True,
                    "response": {"name": SUBSCRIPTION_NAME, "state": "ACTIVE"},
                },
            )
        raise AssertionError(f"unexpected request {method} {url}")

    adapter = _adapter(request_fn=request_fn)
    result = adapter.provision(
        _provision_request(
            provider_trigger_slug=DRIVE_EVENT_TYPE,
            canonical_app_slug="google_drive",
            trigger_config={
                "target_resource": "files/1aaabbbAAABBB111222-_",
                "include_descendants": True,
            },
        ),
    )

    assert result.external_trigger_id == SUBSCRIPTION_NAME
    assert not any(c[1].endswith("/userinfo") for c in calls)
    create_call = next(c for c in calls if c[1].endswith("/subscriptions"))
    assert create_call[2]["json"] == {
        "targetResource": ("//drive.googleapis.com/files/1aaabbbAAABBB111222-_"),
        "eventTypes": [DRIVE_EVENT_TYPE],
        "notificationEndpoint": {"pubsubTopic": PUBSUB_TOPIC},
        "payloadOptions": {"includeResource": False},
        "driveOptions": {"includeDescendants": True},
    }


def test_native_google_provision_chat_space_target() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/subscriptions"):
            return _FakeResponse(
                payload={
                    "done": True,
                    "response": {"name": SUBSCRIPTION_NAME, "state": "ACTIVE"},
                },
            )
        raise AssertionError(f"unexpected request {method} {url}")

    adapter = _adapter(request_fn=request_fn)
    result = adapter.provision(
        _provision_request(
            provider_trigger_slug=CHAT_EVENT_TYPE,
            canonical_app_slug="google_chat",
            trigger_config={"target_resource": "spaces/AAAABBBB"},
        ),
    )

    assert result.external_trigger_id == SUBSCRIPTION_NAME
    assert not any(c[1].endswith("/userinfo") for c in calls)
    create_call = next(c for c in calls if c[1].endswith("/subscriptions"))
    assert create_call[2]["json"] == {
        "targetResource": "//chat.googleapis.com/spaces/AAAABBBB",
        "eventTypes": [CHAT_EVENT_TYPE],
        "notificationEndpoint": {"pubsubTopic": PUBSUB_TOPIC},
        "payloadOptions": {"includeResource": False},
    }


@pytest.mark.parametrize(
    ("slug", "trigger_config", "match"),
    [
        (
            CHAT_BATCH_EVENT_TYPE,
            {},
            "delivery-only",
        ),
        (
            DRIVE_EVENT_TYPE,
            {},
            "target_resource is required",
        ),
    ],
)
def test_native_google_provision_fails_closed_for_batch_or_missing_config(
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


def test_native_google_provision_fails_closed_without_access_token() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected without a token")

    adapter = _adapter(
        request_fn=request_fn,
        credentials=_credentials(access_token=""),
    )
    with pytest.raises(PermissionError):
        adapter.provision(_provision_request())


def test_native_google_provision_fails_closed_when_create_returns_no_name() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        if url.endswith("/userinfo"):
            return _FakeResponse(payload={"sub": "108234567890123456789"})
        return _FakeResponse(payload={"done": True, "response": {}})

    adapter = _adapter(request_fn=request_fn)
    with pytest.raises(RuntimeError, match="no subscription name"):
        adapter.provision(_provision_request())


@pytest.mark.parametrize("status_code", [200, 404, 410])
def test_native_google_delete_removes_subscription_and_tolerates_missing(
    status_code: int,
) -> None:
    calls: list[tuple[str, str]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url))
        return _FakeResponse(status_code=status_code)

    adapter = _adapter(request_fn=request_fn)
    adapter.delete(
        TriggerDeleteRequest(
            external_trigger_id=SUBSCRIPTION_NAME,
            idempotency_key="idem-delete-1",
            connection_id="ic_ws_native_google_google_meet_1",
        ),
    )

    assert calls == [
        (
            "DELETE",
            f"https://workspaceevents.googleapis.com/v1/{SUBSCRIPTION_NAME}",
        ),
    ]


def test_native_google_delete_raises_on_server_error() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(status_code=500, text="boom")

    adapter = _adapter(request_fn=request_fn)
    with pytest.raises(RuntimeError):
        adapter.delete(
            TriggerDeleteRequest(
                external_trigger_id=SUBSCRIPTION_NAME,
                idempotency_key="idem-delete-2",
                connection_id="ic_ws_native_google_google_meet_1",
            ),
        )


def test_native_google_delete_skips_legacy_stub_id() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected for a legacy stub id")

    adapter = _adapter(request_fn=request_fn)
    adapter.delete(
        TriggerDeleteRequest(
            external_trigger_id="ng_deadbeef1234",
            idempotency_key="idem-delete-3",
            connection_id="ic_ws_native_google_google_meet_1",
        ),
    )


def test_native_google_delete_tolerates_missing_credentials() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP call expected without credentials")

    loader = _FakeCredentialLoader(None)
    adapter = NativeGoogleTriggerAdapter(
        credential_loader=loader,  # type: ignore[arg-type]
        webhook_secret="native-google-secret",
        pubsub_topic=PUBSUB_TOPIC,
        request_fn=request_fn,
    )
    adapter.delete(
        TriggerDeleteRequest(
            external_trigger_id=SUBSCRIPTION_NAME,
            idempotency_key="idem-delete-4",
            connection_id="ic_ws_native_google_google_meet_1",
        ),
    )


def test_native_google_normalize_rejects_missing_event_identity() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("normalize must not perform HTTP")

    adapter = _adapter(request_fn=request_fn)
    payload = {
        "provider_trigger_slug": MEET_EVENT_TYPE,
        "data": {"meeting_code": "abc-defg-hij"},
    }
    with pytest.raises(ValueError, match="retry-stable event identity"):
        adapter.normalize_delivery(
            headers={},
            raw_body=json.dumps(payload).encode("utf-8"),
        )


def test_local_native_google_normalize_rejects_missing_event_identity() -> None:
    adapter = LocalNativeGoogleTriggerAdapter(webhook_secret="secret")
    payload = {"provider_trigger_slug": MEET_EVENT_TYPE, "data": {}}
    with pytest.raises(ValueError, match="retry-stable event identity"):
        adapter.normalize_delivery(
            headers={},
            raw_body=json.dumps(payload).encode("utf-8"),
        )


@pytest.mark.parametrize(
    ("status_code", "payload", "error_code"),
    [
        (404, {}, "provider_subscription_missing"),
        (410, {}, "provider_subscription_missing"),
        (
            200,
            {
                "name": SUBSCRIPTION_NAME,
                "state": "ACTIVE",
                "expireTime": (
                    datetime.now(timezone.utc) - timedelta(minutes=5)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "provider_subscription_missing",
        ),
        (
            200,
            {
                "name": SUBSCRIPTION_NAME,
                "state": "SUSPENDED",
                "suspensionReason": "USER_SCOPE_REVOKED",
                "expireTime": (datetime.now(timezone.utc) + timedelta(days=2)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                ),
            },
            "provider_subscription_inactive",
        ),
        (
            200,
            {
                "name": SUBSCRIPTION_NAME,
                "state": "DELETED",
                "expireTime": (datetime.now(timezone.utc) + timedelta(days=2)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                ),
            },
            "provider_subscription_inactive",
        ),
    ],
)
def test_native_google_health_maps_dead_subscription_states(
    status_code: int,
    payload: dict[str, Any],
    error_code: str,
) -> None:
    calls: list[tuple[str, str]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url))
        return _FakeResponse(status_code=status_code, payload=payload)

    loader = _FakeCredentialLoader(_credentials())
    adapter = NativeGoogleTriggerAdapter(
        credential_loader=loader,  # type: ignore[arg-type]
        webhook_secret="native-google-secret",
        pubsub_topic=PUBSUB_TOPIC,
        request_fn=request_fn,
    )
    result = adapter.health(
        external_trigger_id=SUBSCRIPTION_NAME,
        provider_connection_id="google:meet.user@example.com",
        connection_id="ic_ws_native_google_google_meet_1",
    )

    assert result.status == "error"
    assert result.error_code == error_code
    assert loader.calls == ["ic_ws_native_google_google_meet_1"]
    assert calls == [
        (
            "GET",
            f"https://workspaceevents.googleapis.com/v1/{SUBSCRIPTION_NAME}",
        ),
    ]
    assert not any(method == "PATCH" for method, _url in calls)


def test_native_google_health_rejects_connection_only_ok() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        raise AssertionError("no HTTP when subscription id is missing")

    adapter = _adapter(request_fn=request_fn)
    result = adapter.health(
        external_trigger_id=None,
        provider_connection_id="google:meet.user@example.com",
        connection_id="ic_ws_native_google_google_meet_1",
    )
    assert result.status == "error"
    assert result.error_code == "provider_subscription_missing"
