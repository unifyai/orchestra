"""Provider adapter invariants and unified integration catalog sync coverage."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.integration_provider_models import (
    DynamicProviderApp,
    ProviderToolCatalog,
)
from orchestra.integrations.providers.composio import ComposioProviderAdapter
from orchestra.integrations.providers.pagination import ProviderPaginationError
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS
from orchestra.web.api.integrations import operations


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class FakeComposioCatalogAdapter:
    last_auth_config_was_created = False

    def list_toolkits(
        self,
        *,
        page_size: int = 1000,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "slug": "DISCORD",
                "name": "Discord",
                "description": "Discord servers and user data.",
                "category": "communication",
                "logo": "https://cdn.composio.dev/discord.svg",
                "auth_schemes": ["OAUTH2"],
                "version": "20260501_00",
            },
            {
                "slug": "GOOGLEDRIVE",
                "name": "Google Drive",
                "description": "Google Drive files.",
                "category": "files",
                "logo": "https://cdn.composio.dev/google-drive.svg",
                "auth_schemes": ["OAUTH2"],
                "version": "20260502_00",
            },
        ]

    def list_tools(
        self,
        *,
        toolkit_slug: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if toolkit_slug == "DISCORD":
            return [
                {
                    "slug": "DISCORD_LIST_MY_GUILDS",
                    "name": "List my guilds",
                    "description": "List Discord guilds.",
                    "toolkit": {"slug": "DISCORD"},
                    "input_parameters": {"type": "object"},
                    "output_parameters": {"type": "object"},
                    "scopes": ["guilds"],
                },
                {
                    "slug": "DISCORD_SEND_MESSAGE",
                    "name": "Send message",
                    "description": "Send a Discord channel message.",
                    "toolkit": {"slug": "DISCORD"},
                    "input_parameters": {"type": "object"},
                    "output_parameters": {"type": "object"},
                    "scopes": ["messages.write"],
                },
            ][:limit]
        return [
            {
                "slug": "GOOGLEDRIVE_SEARCH_FILES",
                "name": "Search files",
                "description": "Search Google Drive files.",
                "toolkit": {"slug": "GOOGLEDRIVE"},
                "input_parameters": {"type": "object"},
                "output_parameters": {"type": "object"},
                "scopes": ["drive.readonly"],
            },
        ][:limit]

    def get_or_create_auth_config(self, toolkit_slug: str) -> str:
        return f"authcfg_{toolkit_slug.lower()}"


class FakeComposioCatalogAdapterWithAuthConfigFailure(FakeComposioCatalogAdapter):
    def list_toolkits(
        self,
        *,
        page_size: int = 1000,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "slug": "DISCORD",
                "name": "Discord",
                "auth_schemes": ["OAUTH2"],
            },
            {
                "slug": "BROKEN",
                "name": "Broken",
                "auth_schemes": ["OAUTH2"],
            },
        ]

    def list_tools(
        self,
        *,
        toolkit_slug: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if toolkit_slug == "BROKEN":
            return [
                {
                    "slug": "BROKEN_DO_THING",
                    "name": "Do thing",
                    "description": "Do a broken app thing.",
                    "toolkit": {"slug": "BROKEN"},
                    "input_parameters": {"type": "object"},
                    "output_parameters": {"type": "object"},
                    "scopes": [],
                },
            ][:limit]
        return super().list_tools(toolkit_slug=toolkit_slug, limit=limit)

    def get_or_create_auth_config(self, toolkit_slug: str) -> str:
        if toolkit_slug == "BROKEN":
            raise ValueError("Composio rejected managed auth config")
        return super().get_or_create_auth_config(toolkit_slug)


class FakePipedreamCatalogAdapter:
    def list_apps(self, *, has_components: bool | None = None) -> list[dict[str, Any]]:
        assert has_components is True
        return [
            {
                "id": "slack",
                "name_slug": "slack",
                "name": "Slack",
                "description": "Team messaging.",
                "categories": [{"name": "Communication"}],
                "img_src": "https://cdn.pipedream.com/slack.svg",
            },
            {
                "id": "github",
                "name_slug": "github",
                "name": "GitHub",
                "description": "Code hosting.",
            },
        ]

    def list_components(
        self,
        *,
        app: str,
        limit: int | None = None,
        component_type: str | None = None,
    ) -> list[dict[str, Any]]:
        assert component_type == "action"
        if app == "slack":
            return [
                {
                    "key": "slack-send-message",
                    "name": "Send Message",
                    "description": "Send a Slack message.",
                    "props": {"channel": {"type": "string"}},
                },
            ][:limit]
        return [
            {
                "key": "github-list-repositories",
                "name": "List Repositories",
                "description": "List repositories.",
            },
        ][:limit]


def test_composio_full_sync_handler_does_not_eagerly_create_auth_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeDAO:
        def __init__(self, session) -> None:
            self.session = session

        def get_backend(self, backend_id: str):
            assert backend_id == "composio"
            return FakeBackend()

    class NoEagerAuthConfigAdapter(FakeComposioCatalogAdapterWithAuthConfigFailure):
        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            raise AssertionError("full sync should not create Composio auth configs")

    def fake_sync_catalog_rows(session, body):
        assert sorted(app["canonical_app_slug"] for app in body.apps) == [
            "broken",
            "discord",
        ]
        assert len(body.tools) == 2
        return {"apps_upserted": len(body.apps), "tools_upserted": len(body.tools)}

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "IntegrationProviderDAO", FakeDAO)
    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: NoEagerAuthConfigAdapter(),
    )
    monkeypatch.setattr(operations, "_sync_catalog_rows", fake_sync_catalog_rows)

    response = operations._composio_live_catalog_handler(
        session=object(),
        body=operations.IntegrationCatalogSyncRequest(
            backend_id="composio",
            sync_mode="full",
            include_all_managed_apps=True,
            create_auth_configs=True,
            tool_limit_per_app=1,
        ),
    )

    assert response.status == "success"
    assert response.apps_upserted == 2
    assert response.tools_upserted == 2
    assert sorted(response.matched_app_slugs) == ["broken", "discord"]
    assert response.skipped_apps == []


def test_composio_app_only_sync_does_not_fetch_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeDAO:
        def __init__(self, session) -> None:
            self.session = session

        def get_backend(self, backend_id: str):
            assert backend_id == "composio"
            return FakeBackend()

    class AppOnlyAdapter(FakeComposioCatalogAdapter):
        def list_tools(self, *, toolkit_slug: str, limit: int | None = None):
            raise AssertionError("app-only sync must not fetch toolkit tools")

    def fake_sync_catalog_rows(session, body):
        assert len(body.apps) == 2
        assert body.tools == []
        return {"apps_upserted": 2, "tools_upserted": 0}

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "IntegrationProviderDAO", FakeDAO)
    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: AppOnlyAdapter(),
    )
    monkeypatch.setattr(operations, "_sync_catalog_rows", fake_sync_catalog_rows)

    response = operations._composio_live_catalog_handler(
        session=object(),
        body=operations.IntegrationCatalogSyncRequest(
            backend_id="composio",
            sync_mode="full",
            include_all_managed_apps=True,
            sync_tools=False,
        ),
    )

    assert response.status == "success"
    assert response.apps_upserted == 2
    assert response.tools_upserted == 0


def test_composio_connect_lazily_creates_missing_auth_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeApp:
        raw_provider_metadata_json = {}

    class FakeConnection:
        backend_id = "composio"
        provider_app_id = "DISCORD"
        connection_id = "ic_test"
        provider_connection_id = None

    class FakeAdapter:
        def __init__(self) -> None:
            self.created_for: list[str] = []

        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            self.created_for.append(toolkit_slug)
            return "authcfg_discord"

        def create_auth_link(
            self,
            *,
            user_id,
            auth_config_id,
            callback_url=None,
            alias=None,
        ):
            assert auth_config_id == "authcfg_discord"
            assert alias == "ic_test"
            return "https://backend.composio.dev/connect/discord", "ca_discord", None

    adapter = FakeAdapter()
    app = FakeApp()
    conn = FakeConnection()

    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: adapter,
    )

    url = operations._provider_connect_url(
        backend=FakeBackend(),
        app=app,
        owner=operations.OwnerContext(user_id="user-1"),
        connection=conn,
        redirect_url="https://console.example/callback",
    )

    assert url == "https://backend.composio.dev/connect/discord"
    assert adapter.created_for == ["DISCORD"]
    assert app.raw_provider_metadata_json["auth_config_id"] == "authcfg_discord"
    assert conn.provider_connection_id == "ca_discord"


def test_composio_connect_logs_auth_config_creation_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeBackend:
        config_json = {}
        status = "enabled"

    class FakeApp:
        canonical_app_slug = "discord"
        raw_provider_metadata_json = {}

    class FakeConnection:
        backend_id = "composio"
        provider_app_id = "DISCORD"
        canonical_app_slug = "discord"
        connection_id = "ic_failure"

    class FakeProviderResponse:
        status_code = 400
        text = '{"message":"invalid toolkit auth config"}'

    class FakeAdapter:
        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            exc = RuntimeError(f"provider rejected {toolkit_slug}")
            exc.response = FakeProviderResponse()
            raise exc

        def create_auth_link(self, **kwargs):
            raise AssertionError("auth link should not be created after config failure")

    monkeypatch.setattr(
        operations,
        "get_provider_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    caplog.set_level(logging.ERROR, logger=operations.__name__)

    with pytest.raises(RuntimeError, match="provider rejected DISCORD"):
        operations._provider_connect_url(
            backend=FakeBackend(),
            app=FakeApp(),
            owner=operations.OwnerContext(owner_scope="assistant", user_id="user-1"),
            connection=FakeConnection(),
            redirect_url="https://console.example/callback",
        )

    assert "Composio connect failure stage=auth_config_create" in caplog.text
    assert "backend_id=composio" in caplog.text
    assert "provider_app_id=DISCORD" in caplog.text
    assert "canonical_app_slug=discord" in caplog.text
    assert "connection_id=ic_failure" in caplog.text
    assert "provider_status_code=400" in caplog.text
    assert "invalid toolkit auth config" in caplog.text


def test_get_apps_uses_sql_page_and_batched_response_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApp:
        def __init__(self, slug: str, display_name: str) -> None:
            self.backend_id = "composio"
            self.provider_app_id = slug.upper()
            self.canonical_app_slug = slug
            self.display_name = display_name
            self.description = None
            self.category = None
            self.icon_url = None
            self.auth_modes = ["oauth"]
            self.available_scopes_json = []
            self.raw_provider_metadata_json = {"source_type": "third_party"}

    page_apps = [FakeApp("alpha", "Alpha"), FakeApp("beta", "Beta")]

    class FakeDAO:
        def __init__(self, session) -> None:
            self.session = session

        def list_overlays_by_slug(self):
            return {}

        def list_enabled_apps_page(self, **kwargs):
            assert kwargs["limit"] == 2
            assert kwargs["offset"] == 4
            return page_apps

        def count_enabled_apps(self, **kwargs):
            return 1043

        def tool_counts_by_app(self, canonical_app_slugs):
            assert list(canonical_app_slugs) == ["alpha", "beta"]
            return {"alpha": 10, "beta": 20}

        def best_connections_by_app(self, *, owner, canonical_app_slugs):
            assert list(canonical_app_slugs) == ["alpha", "beta"]
            return {}

        def list_enabled_apps(self, **kwargs):
            raise AssertionError("get_apps must not load the full app catalog")

        def tool_count_for_app(self, canonical_app_slug: str):
            raise AssertionError("get_apps must not count tools per app")

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "IntegrationProviderDAO", FakeDAO)

    response = operations.get_apps(
        session=object(),
        body=operations.ProviderAppGetRequest(limit=2, offset=4),
    )

    assert response.total == 1043
    assert [item.canonical_app_slug for item in response.items] == ["alpha", "beta"]
    assert [item.tool_count for item in response.items] == [10, 20]


def test_get_tools_uses_sql_page_for_unconnected_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTool:
        tool_id = "composio:alpha:list_items"
        backend_id = "composio"
        provider_app_id = "ALPHA"
        provider_tool_id = "ALPHA_LIST_ITEMS"
        canonical_name = "primitives.integrations.alpha.list_items"
        function_manager_name = "primitives_integrations__alpha__list_items"
        name = "list_items"
        canonical_app_slug = "alpha"
        display_name = "List Items"
        description = "List items"
        action_class = "read"
        required_scopes_json = []
        confirmation_required = False
        overlay_rank_boost = 0
        enabled_by_default = True

    class FakeApp:
        canonical_app_slug = "alpha"
        display_name = "Alpha"
        icon_url = None

    class FakeDAO:
        def __init__(self, session) -> None:
            self.session = session

        def list_tools_page(self, **kwargs):
            assert kwargs["limit"] == 1
            assert kwargs["offset"] == 3
            return [FakeTool()]

        def count_tools(self, **kwargs):
            return 43324

        def list_apps_by_slug(self, canonical_app_slugs):
            assert list(canonical_app_slugs) == ["alpha"]
            return {"alpha": FakeApp()}

        def best_connections_by_app(self, *, owner, canonical_app_slugs):
            assert list(canonical_app_slugs) == ["alpha"]
            return {}

        def list_tools(self, **kwargs):
            raise AssertionError("get_tools must not load the full tool catalog")

        def list_all_apps(self):
            raise AssertionError("get_tools must not load all apps")

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "IntegrationProviderDAO", FakeDAO)

    response = operations.get_tools(
        session=object(),
        body=operations.ProviderToolGetRequest(
            include_unconnected=True,
            limit=1,
            offset=3,
        ),
    )

    assert response.total == 43324
    assert [item.tool_id for item in response.items] == ["composio:alpha:list_items"]


def test_empty_search_apps_uses_paginated_get_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_apps(session, body):
        assert body.query is None
        return operations.ProviderAppGetResponse(
            items=[
                operations.DynamicIntegrationAppResponse(
                    backend_id="composio",
                    provider_app_id="ALPHA",
                    canonical_app_slug="alpha",
                    display_name="Alpha",
                    source_type="third_party",
                    source_label="Third-party",
                    auth_modes=["oauth"],
                    tool_count=1,
                ),
            ],
            total=1,
            limit=body.limit,
            offset=body.offset,
        )

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "get_apps", fake_get_apps)

    response = operations.search_apps(
        session=object(),
        body=operations.ProviderAppSearchRequest(limit=1, offset=2),
    )

    assert response[0].canonical_app_slug == "alpha"
    assert response[0].match_reason == "all supported integrations"


def test_empty_search_tools_uses_paginated_get_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = operations.ProviderToolSearchResult(
        tool_id="composio:alpha:list_items",
        backend_id="composio",
        provider_app_id="ALPHA",
        provider_tool_id="ALPHA_LIST_ITEMS",
        canonical_name="primitives.integrations.alpha.list_items",
        function_manager_name="primitives_integrations__alpha__list_items",
        app_slug="alpha",
        app_display_name="Alpha",
        tool_display_name="List Items",
        description="List items",
        match_reason="filtered provider tool",
        activation_state="not_connected",
        action_class="read",
    )

    def fake_get_tools(session, body):
        assert body.limit == 1
        assert body.offset == 2
        return operations.ProviderToolGetResponse(
            items=[item],
            total=1,
            limit=body.limit,
            offset=body.offset,
        )

    monkeypatch.setattr(
        operations,
        "seed_default_provider_catalog",
        lambda session: None,
    )
    monkeypatch.setattr(operations, "get_tools", fake_get_tools)

    response = operations.search_tools(
        session=object(),
        body=operations.ProviderToolSearchRequest(
            limit=1,
            offset=2,
            include_unconnected=True,
        ),
    )

    assert response == [item]


def test_composio_adapter_fetches_catalog_and_manages_auth_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_get(url, *, headers, params=None, timeout):
        calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            },
        )
        if url.endswith("/toolkits"):
            return FakeResponse({"items": [{"slug": "DISCORD", "name": "Discord"}]})
        if url.endswith("/tools"):
            return FakeResponse(
                {
                    "items": [
                        {
                            "slug": "DISCORD_LIST_MY_GUILDS",
                            "toolkit": {"slug": "DISCORD"},
                        },
                    ],
                },
            )
        if url.endswith("/auth_configs"):
            return FakeResponse({"items": []})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, *, headers, json, timeout):
        calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            },
        )
        if url.endswith("/auth_configs"):
            return FakeResponse({"id": "authcfg_discord"})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)
    adapter = ComposioProviderAdapter(api_key="composio-key", timeout_seconds=9)

    assert adapter.list_toolkits() == [{"slug": "DISCORD", "name": "Discord"}]
    assert adapter.list_tools(toolkit_slug="DISCORD") == [
        {"slug": "DISCORD_LIST_MY_GUILDS", "toolkit": {"slug": "DISCORD"}},
    ]
    assert adapter.get_or_create_auth_config("DISCORD") == "authcfg_discord"
    assert {call["method"] for call in calls} == {"GET", "POST"}


def test_composio_adapter_uses_bounded_cursor_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor_calls: list[str | None] = []

    def fake_get(url, *, headers, params=None, timeout):
        assert url.endswith("/toolkits")
        cursor_calls.append((params or {}).get("cursor"))
        if not (params or {}).get("cursor"):
            return FakeResponse(
                {"items": [{"slug": "A"}], "next_cursor": "cursor-2", "total_items": 2},
            )
        return FakeResponse(
            {"items": [{"slug": "B"}], "next_cursor": None, "total_items": 2},
        )

    monkeypatch.setattr("requests.get", fake_get)
    adapter = ComposioProviderAdapter(
        api_key="composio-key",
        timeout_seconds=9,
        max_pages=5,
    )

    assert adapter.list_toolkits(page_size=1) == [{"slug": "A"}, {"slug": "B"}]
    assert cursor_calls == [None, "cursor-2"]


def test_composio_adapter_rejects_repeated_pagination_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url, *, headers, params=None, timeout):
        return FakeResponse({"items": [{"slug": "A"}], "next_cursor": "same-cursor"})

    monkeypatch.setattr("requests.get", fake_get)
    adapter = ComposioProviderAdapter(
        api_key="composio-key",
        timeout_seconds=9,
        max_pages=5,
    )

    with pytest.raises(ProviderPaginationError):
        adapter.list_toolkits(page_size=1)


@pytest.mark.anyio
async def test_sync_route_imports_all_composio_apps_without_default_allowlist(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: FakeComposioCatalogAdapter(),
    )

    response = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "composio",
            "tool_limit_per_app": 10,
            "create_auth_configs": True,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["apps_upserted"] == 2
    assert response.json()["tools_upserted"] == 3
    assert response.json()["skipped_apps"] == []
    assert (
        dbsession.query(DynamicProviderApp)
        .filter_by(canonical_app_slug="discord")
        .one()
    )
    assert (
        dbsession.query(DynamicProviderApp)
        .filter_by(canonical_app_slug="google_drive")
        .one()
    )
    assert (
        dbsession.query(ProviderToolCatalog)
        .filter_by(canonical_name="primitives.integrations.discord.send_message")
        .one()
        .confirmation_required
        is True
    )


@pytest.mark.anyio
async def test_sync_route_honors_explicit_composio_subset_and_reports_missing(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: FakeComposioCatalogAdapter(),
    )

    response = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "composio",
            "app_slugs": ["DISCORD", "UNKNOWN_APP"],
            "tool_limit_per_app": 1,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["apps_upserted"] == 1
    assert response.json()["tools_upserted"] == 1
    assert response.json()["skipped_apps"] == [
        {"slug": "UNKNOWN_APP", "reason": "not_found"},
    ]
    assert (
        dbsession.query(DynamicProviderApp)
        .filter_by(canonical_app_slug="discord")
        .one()
    )
    assert (
        dbsession.query(DynamicProviderApp)
        .filter_by(canonical_app_slug="google_drive")
        .one_or_none()
        is None
    )


@pytest.mark.anyio
async def test_composio_full_sync_does_not_eagerly_create_auth_configs(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoEagerAuthConfigAdapter(FakeComposioCatalogAdapterWithAuthConfigFailure):
        def get_or_create_auth_config(self, toolkit_slug: str) -> str:
            raise AssertionError("full sync should not create Composio auth configs")

    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: NoEagerAuthConfigAdapter(),
    )

    response = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "composio",
            "sync_mode": "full",
            "include_all_managed_apps": True,
            "create_auth_configs": True,
            "tool_limit_per_app": 1,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["apps_upserted"] == 2
    assert payload["tools_upserted"] == 2
    assert payload["matched_app_slugs"] == ["discord", "broken"]
    assert payload["skipped_apps"] == []
    assert (
        dbsession.query(DynamicProviderApp)
        .filter_by(canonical_app_slug="discord")
        .one()
    )
    assert (
        dbsession.query(DynamicProviderApp).filter_by(canonical_app_slug="broken").one()
    )


@pytest.mark.anyio
async def test_composio_partial_sync_skips_auth_config_failures(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: FakeComposioCatalogAdapterWithAuthConfigFailure(),
    )

    response = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "composio",
            "app_slugs": ["DISCORD", "BROKEN"],
            "sync_mode": "partial",
            "create_auth_configs": True,
            "tool_limit_per_app": 1,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["apps_upserted"] == 1
    assert payload["tools_upserted"] == 1
    assert payload["matched_app_slugs"] == ["discord"]
    assert payload["skipped_apps"] == [
        {
            "slug": "BROKEN",
            "reason": "auth_config_failed",
            "message": "Composio rejected managed auth config",
        },
    ]
    assert (
        dbsession.query(DynamicProviderApp)
        .filter_by(canonical_app_slug="discord")
        .one()
    )
    assert (
        dbsession.query(DynamicProviderApp)
        .filter_by(canonical_app_slug="broken")
        .one_or_none()
        is None
    )


@pytest.mark.anyio
async def test_sync_route_imports_pipedream_apps_and_actions(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: FakePipedreamCatalogAdapter(),
    )

    response = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "pipedream",
            "app_slugs": ["slack", "missing"],
            "component_limit_per_app": 10,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["apps_upserted"] == 1
    assert response.json()["tools_upserted"] == 1
    assert response.json()["skipped_apps"] == [
        {"slug": "missing", "reason": "not_found"},
    ]
    assert (
        dbsession.query(DynamicProviderApp).filter_by(canonical_app_slug="slack").one()
    )
    tool = (
        dbsession.query(ProviderToolCatalog)
        .filter_by(canonical_name="primitives.integrations.slack.send_message")
        .one()
    )
    assert tool.confirmation_required is True


@pytest.mark.anyio
async def test_sync_route_marks_all_missing_pipedream_subset_failed(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: FakePipedreamCatalogAdapter(),
    )

    response = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "pipedream",
            "app_slugs": ["missing"],
            "sync_mode": "partial",
            "component_limit_per_app": 10,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["apps_upserted"] == 0
    assert payload["tools_upserted"] == 0
    assert payload["requested_app_slugs"] == ["missing"]
    assert payload["matched_app_slugs"] == []
    assert payload["skipped_apps"] == [
        {"slug": "missing", "reason": "not_found"},
    ]
    assert (
        dbsession.query(DynamicProviderApp).filter_by(backend_id="pipedream").count()
        == 0
    )


@pytest.mark.anyio
async def test_live_composio_oauth_connect_route_uses_backend_config(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnectAdapter:
        def create_auth_link(
            self,
            *,
            user_id,
            auth_config_id,
            callback_url=None,
            alias=None,
        ):
            assert user_id == "integration-user"
            assert auth_config_id == "authcfg_discord"
            assert alias and alias.startswith("ic_")
            assert (
                callback_url
                == f"http://localhost:3000/integrations/callback?connection_id={alias}"
            )
            return "https://backend.composio.dev/connect/discord", "ca_discord", None

    monkeypatch.setattr(
        "orchestra.web.api.integrations.operations.get_provider_adapter",
        lambda *_args, **_kwargs: FakeConnectAdapter(),
    )

    backend = await client.patch(
        "/v0/admin/integrations/backends/composio",
        headers=ADMIN_HEADERS,
        json={"status": "enabled"},
    )
    assert backend.status_code == status.HTTP_200_OK, backend.json()

    sync = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "composio",
            "apps": [
                {
                    "provider_app_id": "DISCORD",
                    "canonical_app_slug": "discord",
                    "display_name": "Discord",
                    "auth_modes": ["oauth"],
                    "raw_provider_metadata": {"auth_config_id": "authcfg_discord"},
                },
            ],
        },
    )
    assert sync.status_code == status.HTTP_200_OK, sync.json()

    start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            "owner_scope": "assistant",
            "assistant_id": 123,
            "user_id": "integration-user",
            "canonical_app_slug": "discord",
            "backend_id": "composio",
            "requested_scopes": ["guilds"],
            "auth_mode": "oauth",
            "redirect_url": "http://localhost:3000/integrations/callback",
        },
    )
    assert start.status_code == status.HTTP_200_OK, start.json()
    assert start.json()["connect_url"] == "https://backend.composio.dev/connect/discord"
    assert start.json()["connection"]["provider_connection_id"] == "ca_discord"
