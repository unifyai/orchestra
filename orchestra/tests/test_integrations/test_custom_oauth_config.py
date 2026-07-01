"""Coverage for bring-your-own (custom) OAuth auth configs.

Exercises the Composio adapter payload/extraction, the operations layer that
stores only the returned ``auth_config_id`` (never the client secret), and the
connect flow preferring an operator-configured custom config over
provider-managed auth.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.integrations.providers.composio import ComposioProviderAdapter
from orchestra.web.api.integrations import operations
from orchestra.web.api.integrations.operations import (
    CUSTOM_AUTH_CONFIG_KEY,
    OwnerContext,
    _custom_auth_config_id,
    delete_custom_auth_config,
    list_custom_auth_configs,
    set_custom_auth_config,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            error = requests.HTTPError(f"{self.status_code} Client Error")
            error.response = self  # type: ignore[attr-defined]
            raise error


# --- Adapter payload / extraction -----------------------------------------


def test_create_custom_auth_config_builds_use_custom_auth_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    captured: dict[str, Any] = {}

    def fake_post(url: str, headers=None, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"auth_config": {"id": "ac_custom_1"}})

    monkeypatch.setattr(requests, "post", fake_post)

    adapter = ComposioProviderAdapter(api_key="test-key")
    auth_config_id = adapter.create_custom_auth_config(
        "tiktok",
        client_id="cid",
        client_secret="csecret",
        scopes=["user.info.basic", "video.publish"],
        name="TikTok (custom OAuth)",
    )

    assert auth_config_id == "ac_custom_1"
    assert adapter.last_auth_config_was_created is True
    body = captured["json"]
    assert body["toolkit"] == {"slug": "tiktok"}
    assert body["auth_config"]["type"] == "use_custom_auth"
    assert body["auth_config"]["auth_scheme"] == "OAUTH2"
    creds = body["auth_config"]["credentials"]
    assert creds["client_id"] == "cid"
    assert creds["client_secret"] == "csecret"
    assert creds["scopes"] == ["user.info.basic", "video.publish"]
    # Defaults to Composio's documented callback when none is supplied.
    assert creds["oauth_redirect_uri"] == adapter.default_oauth_callback_url()


def test_create_custom_auth_config_requires_credentials() -> None:
    adapter = ComposioProviderAdapter(api_key="test-key")
    with pytest.raises(ValueError):
        adapter.create_custom_auth_config("tiktok", client_id="", client_secret="x")


def test_create_custom_auth_config_raises_when_no_id_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: _FakeResponse({"auth_config": {}}),
    )
    adapter = ComposioProviderAdapter(api_key="test-key")
    with pytest.raises(ValueError):
        adapter.create_custom_auth_config("tiktok", client_id="c", client_secret="s")


def test_extract_auth_config_id_handles_envelopes() -> None:
    extract = ComposioProviderAdapter._extract_auth_config_id
    assert extract({"auth_config": {"id": "ac_1"}}) == "ac_1"
    assert extract({"auth_config": {"auth_config_id": "ac_2"}}) == "ac_2"
    assert extract({"id": "ac_3"}) == "ac_3"
    assert extract({"auth_config": {}}) is None
    assert extract("not-a-dict") is None


def test_delete_auth_config_tolerates_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    monkeypatch.setattr(requests, "delete", lambda *a, **k: _FakeResponse({}, 404))
    adapter = ComposioProviderAdapter(api_key="test-key")
    # 404 (already gone) must not raise.
    adapter.delete_auth_config("ac_missing")

    monkeypatch.setattr(requests, "delete", lambda *a, **k: _FakeResponse({}, 500))
    with pytest.raises(requests.HTTPError):
        adapter.delete_auth_config("ac_boom")


# --- Operations: store only the id, never the secret -----------------------


class _FakeCustomAuthAdapter:
    """Records custom-auth calls; refuses managed auth to prove precedence."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.auth_link_calls: list[str] = []
        self.next_auth_config_id = "ac_custom_123"

    def default_oauth_callback_url(self) -> str:
        return "https://backend.composio.dev/api/v3.1/toolkits/auth/callback"

    def create_custom_auth_config(
        self,
        toolkit_slug: str,
        *,
        client_id: str,
        client_secret: str,
        auth_scheme: str = "OAUTH2",
        scopes=None,
        name=None,
        oauth_redirect_uri=None,
    ) -> str:
        assert client_id and client_secret
        self.created.append(
            {
                "toolkit_slug": toolkit_slug,
                "auth_scheme": auth_scheme,
                "scopes": scopes,
            },
        )
        return self.next_auth_config_id

    def delete_auth_config(self, auth_config_id: str) -> None:
        self.deleted.append(auth_config_id)

    def create_auth_link(self, *, user_id, auth_config_id, callback_url=None, alias=None):
        self.auth_link_calls.append(auth_config_id)
        return (f"https://consent.example/{auth_config_id}", "acct_1", None)

    def get_or_create_auth_config(self, toolkit_slug: str) -> str:
        raise AssertionError(
            "managed auth must not be used when a custom config exists",
        )


def _enable_composio(dbsession) -> None:
    operations.seed_default_provider_catalog(dbsession)
    from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO

    IntegrationProviderDAO(dbsession).patch_backend("composio", {"status": "enabled"})
    dbsession.flush()


def test_set_custom_auth_config_stores_id_not_secret(
    dbsession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_composio(dbsession)
    fake = _FakeCustomAuthAdapter()
    monkeypatch.setattr(operations, "get_provider_adapter", lambda *a, **k: fake)

    entry = set_custom_auth_config(
        dbsession,
        backend_id="composio",
        toolkit_slug="tiktok",
        client_id="my-client-id",
        client_secret="super-secret",
        scopes=["video.publish"],
        display_name="TikTok",
    )

    assert entry["auth_config_id"] == "ac_custom_123"
    assert entry["managed"] is False
    assert entry["toolkit_slug"] == "TIKTOK"
    # The stored (returned) entry must never carry the secret.
    assert "client_secret" not in entry
    assert "client_id" not in entry

    from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO

    backend = IntegrationProviderDAO(dbsession).get_backend("composio")
    overrides = backend.config_json[CUSTOM_AUTH_CONFIG_KEY]
    assert overrides["TIKTOK"]["auth_config_id"] == "ac_custom_123"
    # No secret is anywhere in the persisted backend config.
    assert "super-secret" not in json.dumps(backend.config_json)
    assert "my-client-id" not in json.dumps(backend.config_json)


def test_list_and_delete_custom_auth_config(
    dbsession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_composio(dbsession)
    fake = _FakeCustomAuthAdapter()
    monkeypatch.setattr(operations, "get_provider_adapter", lambda *a, **k: fake)

    set_custom_auth_config(
        dbsession,
        backend_id="composio",
        toolkit_slug="tiktok",
        client_id="cid",
        client_secret="secret",
    )

    listed = list_custom_auth_configs(dbsession, backend_id="composio")
    assert len(listed) == 1
    assert listed[0]["toolkit_slug"] == "TIKTOK"
    assert listed[0]["auth_config_id"] == "ac_custom_123"

    delete_custom_auth_config(dbsession, backend_id="composio", toolkit_slug="tiktok")
    assert list_custom_auth_configs(dbsession, backend_id="composio") == []
    # The provider-side config is cleaned up too.
    assert fake.deleted == ["ac_custom_123"]


def test_set_custom_auth_config_replaces_and_cleans_up_old(
    dbsession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_composio(dbsession)
    fake = _FakeCustomAuthAdapter()
    monkeypatch.setattr(operations, "get_provider_adapter", lambda *a, **k: fake)

    set_custom_auth_config(
        dbsession,
        backend_id="composio",
        toolkit_slug="tiktok",
        client_id="cid",
        client_secret="secret",
    )
    fake.next_auth_config_id = "ac_custom_456"
    entry = set_custom_auth_config(
        dbsession,
        backend_id="composio",
        toolkit_slug="tiktok",
        client_id="cid2",
        client_secret="secret2",
    )

    assert entry["auth_config_id"] == "ac_custom_456"
    # The superseded provider config is deleted best-effort.
    assert fake.deleted == ["ac_custom_123"]


def test_set_custom_auth_config_unknown_backend(dbsession) -> None:
    operations.seed_default_provider_catalog(dbsession)
    with pytest.raises(ValueError):
        set_custom_auth_config(
            dbsession,
            backend_id="does-not-exist",
            toolkit_slug="tiktok",
            client_id="c",
            client_secret="s",
        )


# --- Connect flow prefers the custom config over managed auth --------------


def test_custom_auth_config_id_lookup() -> None:
    config = {
        CUSTOM_AUTH_CONFIG_KEY: {
            "TIKTOK": {"auth_config_id": "ac_custom_789"},
        },
    }
    assert _custom_auth_config_id(config, "TIKTOK") == "ac_custom_789"
    assert _custom_auth_config_id(config, "GMAIL") is None
    assert _custom_auth_config_id({}, "TIKTOK") is None


def test_connect_url_uses_custom_auth_config(
    dbsession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_composio(dbsession)
    fake = _FakeCustomAuthAdapter()
    monkeypatch.setattr(operations, "get_provider_adapter", lambda *a, **k: fake)

    set_custom_auth_config(
        dbsession,
        backend_id="composio",
        toolkit_slug="tiktok",
        client_id="cid",
        client_secret="secret",
    )

    from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO

    backend = IntegrationProviderDAO(dbsession).get_backend("composio")
    connection = IntegrationConnection(
        connection_id="conn-tiktok-1",
        owner_scope="assistant",
        assistant_id=4242,
        canonical_app_slug="tiktok",
        backend_id="composio",
        provider_app_id="TIKTOK",
        status="pending",
    )
    owner = OwnerContext(owner_scope="assistant", assistant_id=4242)

    url = operations._provider_connect_url(
        backend=backend,
        app=None,
        owner=owner,
        connection=connection,
        redirect_url="https://console.example/return",
    )

    # The consent URL is built from the operator's custom auth config, and the
    # managed-auth path (which raises in the fake) is never reached.
    assert url == "https://consent.example/ac_custom_123"
    assert fake.auth_link_calls == ["ac_custom_123"]
    assert connection.provider_connection_id == "acct_1"
