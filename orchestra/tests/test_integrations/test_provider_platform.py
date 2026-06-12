"""API-first tests for the provider-neutral integration control plane."""

from __future__ import annotations

import uuid

import pytest
from fastapi import status
from httpx import AsyncClient
from scripts.cloud_bootstrap_provider_integrations import (
    _sync_diagnostics,
    apply_plan,
    provider_plans,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.integration_provider_models import (
    DynamicProviderApp,
    IntegrationBackend,
    ProviderActionAudit,
    ProviderToolCatalog,
)
from orchestra.integrations.providers.composio import ComposioProviderAdapter
from orchestra.integrations.providers.local_echo import LocalEchoProviderAdapter
from orchestra.integrations.providers.pipedream import PipedreamProviderAdapter
from orchestra.integrations.providers.registry import get_provider_adapter
from orchestra.tests.utils import ADMIN_HEADERS, HEADERS
from orchestra.web.api.integrations.operations import (
    create_confirmation_token,
    seed_default_provider_catalog,
)


@pytest.fixture(autouse=True)
def _clear_provider_env_for_local_echo_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("PIPEDREAM_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_ID", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_SECRET", raising=False)


def _owner_payload(assistant_id: int, user_id: str = "api-user") -> dict[str, object]:
    return {
        "owner_scope": "assistant",
        "assistant_id": assistant_id,
        "user_id": user_id,
    }


def _owner_query(assistant_id: int, user_id: str = "api-user") -> str:
    return f"owner_scope=assistant&assistant_id={assistant_id}&user_id={user_id}"


async def _sync_integrations(
    client: AsyncClient,
    *,
    backend_id: str = "composio",
    app_slug: str = "hubspot",
    display_name: str = "HubSpot",
    tool_name: str = "search_contacts",
    tool_display_name: str = "Search HubSpot contacts",
    action_class: str = "read",
    required_scopes: list[str] | None = None,
    source_type: str = "third_party",
) -> dict:
    response = await client.post(
        "/v0/admin/integrations/sync",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": backend_id,
            "source_type": source_type,
            "apps": [
                {
                    "provider_app_id": app_slug,
                    "canonical_app_slug": app_slug,
                    "display_name": display_name,
                    "description": f"{display_name} catalog entry.",
                    "category": "CRM" if source_type == "third_party" else "native",
                    "auth_modes": (
                        ["api_key"] if source_type == "third_party" else ["native"]
                    ),
                    "tier": "api",
                    "quality": "gold",
                    "function_names": (
                        [f"{app_slug}_sync"] if source_type == "native" else []
                    ),
                    "required_secrets": (
                        [f"{app_slug.upper()}_TOKEN"] if source_type == "native" else []
                    ),
                    "tags": [app_slug, "synced"],
                },
            ],
            "tools": (
                []
                if source_type == "native"
                else [
                    {
                        "provider_app_id": app_slug,
                        "canonical_app_slug": app_slug,
                        "provider_tool_id": f"{app_slug}.{tool_name}",
                        "name": tool_name,
                        "display_name": tool_display_name,
                        "description": f"{tool_display_name} for synced catalog tests.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                        "output_schema": {"type": "object"},
                        "required_scopes": (
                            required_scopes if required_scopes is not None else ["read"]
                        ),
                        "action_class": action_class,
                        "confirmation_required": action_class
                        in {"write", "destructive", "bulk_export"},
                    },
                ]
            ),
        },
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    return response.json()


def test_backend_bootstrap_is_idempotent_and_does_not_seed_catalog(
    dbsession: Session,
) -> None:
    seed_default_provider_catalog(dbsession)
    first_backend_count = dbsession.query(IntegrationBackend).count()

    pipedream = (
        dbsession.query(IntegrationBackend).filter_by(backend_id="pipedream").one()
    )
    pipedream.status = "enabled"
    pipedream.config_json = {"timeout_seconds": 12}
    dbsession.flush()

    seed_default_provider_catalog(dbsession)

    assert dbsession.query(IntegrationBackend).count() == first_backend_count
    assert dbsession.query(DynamicProviderApp).count() == 0
    assert dbsession.query(ProviderToolCatalog).count() == 0
    assert (
        dbsession.query(IntegrationBackend)
        .filter_by(backend_id="pipedream")
        .one()
        .status
        == "enabled"
    )
    assert (
        dbsession.query(IntegrationBackend)
        .filter_by(backend_id="pipedream")
        .one()
        .config_json["timeout_seconds"]
        == 12
    )


def test_provider_registry_uses_deployment_env_for_live_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPOSIO_API_KEY", "composio-key")
    monkeypatch.delenv("PIPEDREAM_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_ID", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_SECRET", raising=False)

    composio = get_provider_adapter("composio", backend_status="enabled")
    pipedream_disabled = get_provider_adapter("pipedream", backend_status="disabled")

    assert isinstance(composio, ComposioProviderAdapter)
    assert isinstance(pipedream_disabled, LocalEchoProviderAdapter)

    monkeypatch.setenv("PIPEDREAM_CLIENT_ID", "pd-client")
    monkeypatch.setenv("PIPEDREAM_CLIENT_SECRET", "pd-secret")
    pipedream = get_provider_adapter("pipedream", backend_status="enabled")
    assert isinstance(pipedream, PipedreamProviderAdapter)


def test_provider_catalog_unique_constraints(dbsession: Session) -> None:
    dbsession.add(
        IntegrationBackend(
            backend_id="duplicate-backend",
            kind="custom",
            environment="test",
            display_name="Duplicate",
        ),
    )
    dbsession.flush()
    dbsession.add(
        IntegrationBackend(
            backend_id="duplicate-backend",
            kind="custom",
            environment="test",
            display_name="Duplicate Again",
        ),
    )
    with pytest.raises(IntegrityError):
        dbsession.flush()
    dbsession.rollback()

    dbsession.add_all(
        [
            DynamicProviderApp(
                backend_id="custom",
                provider_app_id="app-one",
                canonical_app_slug="same-slug",
                display_name="App One",
            ),
            DynamicProviderApp(
                backend_id="custom",
                provider_app_id="app-two",
                canonical_app_slug="same-slug",
                display_name="App Two",
            ),
        ],
    )
    with pytest.raises(IntegrityError):
        dbsession.flush()
    dbsession.rollback()


async def test_bootstrap_state_admin_api_round_trips(client: AsyncClient) -> None:
    payload = {
        "environment": "staging",
        "backend_id": "composio",
        "desired_hash": "abc123",
        "desired_config": {
            "backend": {"backend_id": "composio", "status": "enabled"},
        },
        "last_status": "success",
        "apps_upserted": 3,
        "tools_upserted": 12,
        "last_sync_diagnostics": {
            "sync_mode": "partial",
            "requested_app_slugs": ["SLACK", "MISSING"],
            "matched_app_slugs": ["slack"],
            "skipped_apps": [{"slug": "MISSING", "reason": "not_found"}],
            "auth_configs_created": 1,
            "cache_version": "test-cache",
            "warning": "one app skipped",
        },
    }

    saved = await client.put(
        "/v0/admin/integrations/bootstrap-state",
        headers=ADMIN_HEADERS,
        json=payload,
    )
    assert saved.status_code == status.HTTP_200_OK, saved.json()
    assert saved.json()["desired_hash"] == "abc123"
    assert saved.json()["last_synced_at"] is not None

    fetched = await client.get(
        "/v0/admin/integrations/bootstrap-state",
        headers=ADMIN_HEADERS,
        params={"environment": "staging", "backend_id": "composio"},
    )
    assert fetched.status_code == status.HTTP_200_OK, fetched.json()
    assert fetched.json()["desired_config"] == payload["desired_config"]
    assert fetched.json()["sync_mode"] == "partial"
    assert fetched.json()["requested_app_slugs"] == ["SLACK", "MISSING"]
    assert fetched.json()["matched_app_slugs"] == ["slack"]
    assert fetched.json()["skipped_apps"] == [
        {"slug": "MISSING", "reason": "not_found"},
    ]
    assert fetched.json()["auth_configs_created"] == 1
    assert fetched.json()["cache_version"] == "test-cache"
    assert fetched.json()["last_sync_warning"] == "one app skipped"

    updated_payload = {
        **payload,
        "desired_hash": "def456",
        "last_status": "failed",
        "last_error": "provider catalog unavailable",
    }
    updated = await client.put(
        "/v0/admin/integrations/bootstrap-state",
        headers=ADMIN_HEADERS,
        json=updated_payload,
    )
    assert updated.status_code == status.HTTP_200_OK, updated.json()
    assert updated.json()["id"] == saved.json()["id"]
    assert updated.json()["desired_hash"] == "def456"
    assert updated.json()["last_status"] == "failed"
    assert updated.json()["last_error"] == "provider catalog unavailable"


def test_cloud_bootstrap_manifest_hash_is_stable() -> None:
    manifest = {
        "schema_version": 1,
        "environment": "staging",
        "providers": {
            "composio": {
                "status": "enabled",
                "sync": {
                    "mode": "partial",
                    "app_slugs": ["GMAIL", "SLACK"],
                    "tool_limit_per_app": 25,
                },
            },
        },
    }

    first = provider_plans(manifest)[0]
    second = provider_plans(manifest)[0]

    assert first.desired_hash == second.desired_hash
    assert first.sync_payload is not None
    assert first.sync_payload["sync_mode"] == "partial"
    assert first.desired_config["sync"]["mode"] == "partial"
    assert first.sync_payload["cache_version"].startswith(
        "cloud-bootstrap-staging-composio-",
    )


def test_cloud_bootstrap_manifest_supports_manifest_defined_provider_ids() -> None:
    manifest = {
        "schema_version": 1,
        "environment": "staging",
        "providers": {
            "custom_provider": {
                "kind": "custom",
                "status": "enabled",
                "display_name": "Custom Provider",
                "sync": {
                    "mode": "partial",
                    "app_slugs": ["custom_app"],
                    "tool_limit_per_app": 0,
                },
            },
        },
    }

    plan = provider_plans(manifest)[0]

    assert plan.backend_id == "custom_provider"
    assert plan.backend_payload["kind"] == "custom"
    assert plan.sync_payload is not None
    assert plan.sync_payload["backend_id"] == "custom_provider"
    assert plan.sync_payload["tool_limit_per_app"] == 0


def test_cloud_bootstrap_skips_unchanged_successful_sync() -> None:
    manifest = {
        "schema_version": 1,
        "environment": "production",
        "providers": {
            "pipedream": {
                "status": "enabled",
                "sync": {
                    "mode": "partial",
                    "app_slugs": ["slack"],
                    "component_limit_per_app": 20,
                },
            },
        },
    }
    plan = provider_plans(manifest)[0]

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.state_updates: list[dict] = []

        def request(self, method: str, path: str, payload=None):
            self.calls.append((method, path))
            return {}

        def bootstrap_state(self, *, environment: str, backend_id: str):
            return {
                "environment": environment,
                "backend_id": backend_id,
                "desired_hash": plan.desired_hash,
                "last_status": "success",
            }

        def put_bootstrap_state(self, **kwargs):
            self.state_updates.append(kwargs)

    client = FakeClient()

    result = apply_plan(
        client=client,
        environment="production",
        plan=plan,
    )

    assert result == "skipped"
    assert client.calls == [("POST", "/admin/integrations/backends")]
    assert client.state_updates[0]["status"] == "skipped"
    assert (
        client.state_updates[0]["result"]["warning"]
        == "Manifest hash already applied; catalog sync skipped."
    )


def test_cloud_bootstrap_full_skip_drops_stale_batch_diagnostics() -> None:
    manifest = {
        "schema_version": 1,
        "environment": "staging",
        "providers": {
            "composio": {
                "status": "enabled",
                "sync": {
                    "mode": "full",
                    "include_all_managed_apps": True,
                    "tool_limit_per_app": 0,
                    "create_auth_configs": False,
                },
            },
        },
    }
    plan = provider_plans(manifest)[0]

    class FakeClient:
        def __init__(self) -> None:
            self.state_updates: list[dict] = []

        def request(self, method: str, path: str, payload=None):
            return {}

        def bootstrap_state(self, *, environment: str, backend_id: str):
            return {
                "environment": environment,
                "backend_id": backend_id,
                "desired_hash": plan.desired_hash,
                "last_status": "success",
                "apps_upserted": 1043,
                "tools_upserted": 43133,
                "last_sync_diagnostics": {
                    "cache_version": "cloud-bootstrap-staging-composio-abc123",
                    "skipped_apps": [{"slug": "STALE", "reason": "not_found"}],
                    "matched_app_slugs": ["stale"],
                },
            }

        def put_bootstrap_state(self, **kwargs):
            self.state_updates.append(kwargs)

    client = FakeClient()

    result = apply_plan(
        client=client,
        environment="staging",
        plan=plan,
    )

    assert result == "skipped"
    skip_result = client.state_updates[0]["result"]
    assert skip_result["apps_upserted"] == 1043
    assert skip_result["tools_upserted"] == 43133
    assert "skipped_apps" not in skip_result
    assert "matched_app_slugs" not in skip_result
    diagnostics = _sync_diagnostics(plan=plan, result=skip_result)
    assert diagnostics["sync_mode"] == "full"
    assert diagnostics["requested_app_slugs"] == []
    assert "skipped_apps" not in diagnostics
    assert "matched_app_slugs" not in diagnostics


def test_cloud_bootstrap_batches_composio_full_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {
        "schema_version": 1,
        "environment": "staging",
        "providers": {
            "composio": {
                "status": "enabled",
                "sync": {
                    "mode": "full",
                    "include_all_managed_apps": True,
                    "tool_limit_per_app": 0,
                    "create_auth_configs": False,
                },
            },
        },
    }
    plan = provider_plans(manifest)[0]

    class FakeClient:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, dict | None]] = []
            self.state_updates: list[dict] = []

        def request(self, method: str, path: str, payload=None):
            self.requests.append((method, path, payload))
            if path == "/admin/integrations/backends":
                return {}
            if payload.get("sync_tools") is False:
                return {
                    "status": "success",
                    "apps_upserted": 3,
                    "tools_upserted": 0,
                    "requested_app_slugs": [],
                    "matched_app_slugs": ["alpha", "beta", "gamma"],
                    "sync_mode": "full",
                    "cache_version": payload["cache_version"],
                }
            return {
                "status": "success",
                "apps_upserted": len(payload["app_slugs"]),
                "tools_upserted": len(payload["app_slugs"]) * 10,
                "skipped_apps": [{"slug": "STALE", "reason": "not_found"}],
                "requested_app_slugs": payload["app_slugs"],
                "matched_app_slugs": payload["app_slugs"],
                "sync_mode": "partial",
                "cache_version": payload["cache_version"],
            }

        def bootstrap_state(self, *, environment: str, backend_id: str):
            return None

        def put_bootstrap_state(self, **kwargs):
            self.state_updates.append(kwargs)

    monkeypatch.setenv("ORCHESTRA_INTEGRATION_BOOTSTRAP_BATCH_SIZE", "2")
    client = FakeClient()

    result = apply_plan(
        client=client,
        environment="staging",
        plan=plan,
    )

    sync_requests = [
        payload
        for _method, path, payload in client.requests
        if path == "/admin/integrations/sync"
    ]
    assert result == "synced"
    assert len(sync_requests) == 3
    assert sync_requests[0]["sync_tools"] is False
    assert sync_requests[1]["app_slugs"] == ["ALPHA", "BETA"]
    assert sync_requests[2]["app_slugs"] == ["GAMMA"]
    assert len(client.state_updates) >= 3
    assert client.state_updates[-1]["result"]["apps_upserted"] == 3
    assert client.state_updates[-1]["result"]["tools_upserted"] == 30
    assert client.state_updates[-1]["result"]["sync_mode"] == "full"
    assert client.state_updates[-1]["result"]["requested_app_slugs"] == []
    final_diagnostics = _sync_diagnostics(
        plan=plan,
        result=client.state_updates[-1]["result"],
    )
    assert final_diagnostics["sync_mode"] == "full"
    assert final_diagnostics["requested_app_slugs"] == []
    assert "skipped_apps" not in final_diagnostics
    assert "matched_app_slugs" not in final_diagnostics

    partial_plan = provider_plans(
        {
            "schema_version": 1,
            "environment": "staging",
            "providers": {
                "composio": {
                    "status": "enabled",
                    "sync": {
                        "mode": "partial",
                        "app_slugs": ["hubspot"],
                    },
                },
            },
        },
    )[0]
    partial_diagnostics = _sync_diagnostics(
        plan=partial_plan,
        result={"status": "success", "matched_app_slugs": ["hubspot"]},
    )
    assert partial_diagnostics["matched_app_slugs"] == ["hubspot"]


@pytest.mark.anyio
async def test_admin_backend_config_and_catalog_sync_routes(
    client: AsyncClient,
) -> None:
    backend_response = await client.post(
        "/v0/admin/integrations/backends",
        headers=ADMIN_HEADERS,
        json={
            "backend_id": "pipedream",
            "kind": "pipedream",
            "environment": "prod",
            "display_name": "Pipedream",
            "status": "enabled",
            "config_json": {"timeout_seconds": 15},
        },
    )
    assert backend_response.status_code == status.HTTP_200_OK, backend_response.json()
    assert backend_response.json()["config_json"]["timeout_seconds"] == 15

    patch_response = await client.patch(
        "/v0/admin/integrations/backends/pipedream",
        headers=ADMIN_HEADERS,
        json={"status": "disabled"},
    )
    assert patch_response.status_code == status.HTTP_200_OK, patch_response.json()
    assert patch_response.json()["status"] == "disabled"

    await client.patch(
        "/v0/admin/integrations/backends/pipedream",
        headers=ADMIN_HEADERS,
        json={"status": "enabled"},
    )
    sync_response = await _sync_integrations(
        client,
        backend_id="pipedream",
        app_slug="linear",
        display_name="Linear",
        tool_name="list_issues",
        tool_display_name="List Linear issues",
    )
    assert sync_response["apps_upserted"] == 1
    assert sync_response["tools_upserted"] == 1

    bootstrap = await client.put(
        "/v0/admin/integrations/bootstrap-state",
        headers=ADMIN_HEADERS,
        json={
            "environment": "prod",
            "backend_id": "pipedream",
            "desired_hash": "pipedream-hash",
            "desired_config": {
                "schema_version": 1,
                "environment": "prod",
                "backend": backend_response.json(),
                "sync": {
                    "mode": "partial",
                    "app_slugs": ["linear"],
                    "component_limit_per_app": 0,
                },
            },
            "last_status": "success",
            "apps_upserted": 1,
            "tools_upserted": 1,
            "last_sync_diagnostics": {
                "sync_mode": "partial",
                "requested_app_slugs": ["linear"],
                "matched_app_slugs": ["linear"],
                "skipped_apps": [],
                "cache_version": "test-cache",
            },
        },
    )
    assert bootstrap.status_code == status.HTTP_200_OK, bootstrap.json()

    status_response = await client.get(
        "/v0/admin/integrations/backends/status",
        headers=ADMIN_HEADERS,
        params={"environment": "prod"},
    )
    assert status_response.status_code == status.HTTP_200_OK, status_response.json()
    pipedream_status = next(
        item
        for item in status_response.json()
        if item["backend"]["backend_id"] == "pipedream"
    )
    assert pipedream_status["desired_hash"] == "pipedream-hash"
    assert pipedream_status["sync_mode"] == "partial"
    assert pipedream_status["requested_app_slugs"] == ["linear"]
    assert pipedream_status["matched_app_slugs"] == ["linear"]
    assert pipedream_status["catalog_app_count"] == 1
    assert pipedream_status["catalog_tool_count"] == 1


@pytest.mark.anyio
async def test_backend_status_is_the_only_catalog_visibility_gate(
    client: AsyncClient,
) -> None:
    await _sync_integrations(
        client,
        backend_id="pipedream",
        app_slug="linear",
        display_name="Linear",
        tool_name="list_issues",
        tool_display_name="List Linear issues",
    )

    hidden = await client.get(
        "/v0/integrations/apps/search",
        headers=HEADERS,
        params={"query": "Linear"},
    )
    assert hidden.status_code == status.HTTP_200_OK, hidden.json()
    assert hidden.json() == []

    disabled_connect = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=11),
            "canonical_app_slug": "linear",
            "backend_id": "pipedream",
            "requested_scopes": ["read"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert disabled_connect.status_code == status.HTTP_404_NOT_FOUND
    assert "disabled" in disabled_connect.json()["detail"]

    enabled = await client.patch(
        "/v0/admin/integrations/backends/pipedream",
        headers=ADMIN_HEADERS,
        json={"status": "enabled"},
    )
    assert enabled.status_code == status.HTTP_200_OK, enabled.json()

    visible = await client.get(
        "/v0/integrations/apps/search",
        headers=HEADERS,
        params={"query": "Linear"},
    )
    assert visible.status_code == status.HTTP_200_OK, visible.json()
    assert visible.json()[0]["canonical_app_slug"] == "linear"


@pytest.mark.anyio
async def test_native_app_sync_search_and_connection_rejection(
    client: AsyncClient,
) -> None:
    await _sync_integrations(
        client,
        backend_id="unity_native",
        app_slug="matterport",
        display_name="Matterport",
        source_type="native",
    )

    page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={"source_type": "native", "query": "Matterport"},
    )
    assert page.status_code == status.HTTP_200_OK, page.json()
    assert page.json()["total"] == 1
    assert page.json()["items"][0]["source_label"] == "Native"
    assert page.json()["items"][0]["native_metadata"]["tier"] == "api"

    third_party_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={"source_type": "third_party", "query": "Matterport"},
    )
    assert third_party_page.status_code == status.HTTP_200_OK, third_party_page.json()
    assert third_party_page.json()["total"] == 0

    rejected = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=22),
            "canonical_app_slug": "matterport",
            "backend_id": "unity_native",
            "requested_scopes": [],
            "auth_mode": "native",
        },
    )
    assert rejected.status_code == status.HTTP_404_NOT_FOUND
    assert "Native Unity-deploy integrations" in rejected.json()["detail"]


@pytest.mark.anyio
async def test_connection_start_derives_requested_scopes_generically(
    client: AsyncClient,
) -> None:
    assistant_id = 120_000 + (uuid.uuid4().int % 1000)
    await _sync_integrations(
        client,
        app_slug="scope_slack",
        display_name="Scope Slack",
        tool_name="send_message",
        required_scopes=["chat:write"],
    )
    await _sync_integrations(
        client,
        app_slug="scope_mail",
        display_name="Scope Mail",
        tool_name="fetch_messages",
        required_scopes=[
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
    )
    await _sync_integrations(
        client,
        app_slug="scope_free",
        display_name="Scope Free",
        tool_name="ping",
        required_scopes=[],
    )

    slack_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_slack",
            "backend_id": "composio",
            "requested_scopes": [],
            "auth_mode": "oauth",
        },
    )
    assert slack_start.status_code == status.HTTP_200_OK, slack_start.json()
    assert slack_start.json()["requested_scopes"] == ["chat:write"]
    assert slack_start.json()["connection"]["granted_scopes"] == ["chat:write"]

    slack_id = slack_start.json()["connection"]["connection_id"]
    slack_complete = await client.post(
        f"/v0/integrations/connections/{slack_id}/complete",
        headers=HEADERS,
        json={
            "provider_connection_id": "provider-scope-slack",
            "granted_scopes": [],
            "status": "connected",
        },
    )
    assert slack_complete.status_code == status.HTTP_200_OK, slack_complete.json()
    assert slack_complete.json()["granted_scopes"] == ["chat:write"]

    connected_tools = await client.get(
        "/v0/integrations/tools",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_slack",
            "activation_state": "connected_ready",
        },
    )
    assert connected_tools.status_code == status.HTTP_200_OK, connected_tools.json()
    assert connected_tools.json()["total"] == 1
    assert connected_tools.json()["items"][0]["activation_state"] == "connected_ready"

    mail_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_mail",
            "backend_id": "composio",
            "requested_scopes": [],
            "auth_mode": "oauth",
        },
    )
    assert mail_start.status_code == status.HTTP_200_OK, mail_start.json()
    assert mail_start.json()["requested_scopes"] == [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]

    explicit_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_mail",
            "backend_id": "composio",
            "requested_scopes": ["https://mail.google.com/"],
            "auth_mode": "oauth",
        },
    )
    assert explicit_start.status_code == status.HTTP_200_OK, explicit_start.json()
    assert explicit_start.json()["requested_scopes"] == ["https://mail.google.com/"]

    scope_free_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_free",
            "backend_id": "composio",
            "requested_scopes": [],
            "auth_mode": "oauth",
        },
    )
    assert scope_free_start.status_code == status.HTTP_200_OK, scope_free_start.json()
    assert scope_free_start.json()["requested_scopes"] == []
    assert scope_free_start.json()["connection"]["granted_scopes"] == []

    api_key_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_mail",
            "backend_id": "composio",
            "requested_scopes": [],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert api_key_start.status_code == status.HTTP_200_OK, api_key_start.json()
    assert api_key_start.json()["requested_scopes"] == [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    assert api_key_start.json()["connection"]["granted_scopes"] == [
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]

    api_key_tools = await client.get(
        "/v0/integrations/tools",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_mail",
            "activation_state": "connected_ready",
        },
    )
    assert api_key_tools.status_code == status.HTTP_200_OK, api_key_tools.json()
    assert api_key_tools.json()["total"] == 1

    api_key_scope_free_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_free",
            "backend_id": "composio",
            "requested_scopes": [],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert (
        api_key_scope_free_start.status_code == status.HTTP_200_OK
    ), api_key_scope_free_start.json()
    assert api_key_scope_free_start.json()["requested_scopes"] == []
    assert api_key_scope_free_start.json()["connection"]["granted_scopes"] == []


@pytest.mark.anyio
async def test_app_catalog_status_filters_facets_and_summary_payload(
    client: AsyncClient,
) -> None:
    assistant_id = 90_000 + (uuid.uuid4().int % 1000)
    app_specs = [
        ("asana", "Asana Tasks", "list_tasks", ["tasks.read"]),
        ("dropbox", "Dropbox Files", "list_files", ["files.read"]),
        ("hubspot", "HubSpot CRM", "search_contacts", ["crm.objects.contacts.read"]),
        ("jira", "Jira Issues", "list_issues", ["issues.read"]),
        ("notion", "Notion Docs", "list_pages", ["pages.read"]),
        ("slack", "Slack Chat", "send_message", ["chat:write"]),
    ]
    for slug, display_name, tool_name, scopes in app_specs:
        await _sync_integrations(
            client,
            app_slug=slug,
            display_name=display_name,
            tool_name=tool_name,
            tool_display_name=f"{display_name} tool",
            required_scopes=scopes,
        )
    await _sync_integrations(
        client,
        app_slug="matterport",
        display_name="Matterport Native",
        source_type="native",
    )

    configured_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "hubspot",
            "backend_id": "composio",
            "requested_scopes": ["crm.objects.contacts.read"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert configured_start.status_code == status.HTTP_200_OK, configured_start.json()

    connected_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "slack",
            "backend_id": "composio",
            "requested_scopes": ["chat:write"],
            "auth_mode": "oauth",
        },
    )
    assert connected_start.status_code == status.HTTP_200_OK, connected_start.json()
    connected_id = connected_start.json()["connection"]["connection_id"]
    connected_complete = await client.post(
        f"/v0/integrations/connections/{connected_id}/complete",
        headers=HEADERS,
        json={
            "provider_connection_id": "provider-slack",
            "granted_scopes": ["chat:write"],
            "status": "connected",
        },
    )
    assert (
        connected_complete.status_code == status.HTTP_200_OK
    ), connected_complete.json()

    missing_scope_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "jira",
            "backend_id": "composio",
            "requested_scopes": ["issues.comment"],
            "auth_mode": "oauth",
        },
    )
    assert (
        missing_scope_start.status_code == status.HTTP_200_OK
    ), missing_scope_start.json()
    missing_scope_id = missing_scope_start.json()["connection"]["connection_id"]
    missing_scope_complete = await client.post(
        f"/v0/integrations/connections/{missing_scope_id}/complete",
        headers=HEADERS,
        json={
            "provider_connection_id": "provider-jira",
            "granted_scopes": [],
            "status": "connected",
        },
    )
    assert (
        missing_scope_complete.status_code == status.HTTP_200_OK
    ), missing_scope_complete.json()

    pending_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "notion",
            "backend_id": "composio",
            "requested_scopes": ["pages.read"],
            "auth_mode": "oauth",
        },
    )
    assert pending_start.status_code == status.HTTP_200_OK, pending_start.json()

    connected_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params=[
            ("owner_scope", "assistant"),
            ("assistant_id", str(assistant_id)),
            ("user_id", "api-user"),
            ("source_type", "third_party"),
            ("status", "connected"),
            ("status", "configured"),
            ("limit", "100"),
            ("offset", "0"),
        ],
    )
    assert connected_page.status_code == status.HTTP_200_OK, connected_page.json()
    connected_body = connected_page.json()
    assert connected_body["total"] == 2
    assert {item["connection_status"] for item in connected_body["items"]} == {
        "connected",
        "configured",
    }

    comma_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "source_type": "third_party",
            "status": "connected,configured",
        },
    )
    assert comma_page.status_code == status.HTTP_200_OK, comma_page.json()
    assert comma_page.json()["total"] == 2

    grouped_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "source_type": "third_party",
            "status_group": "connected",
        },
    )
    assert grouped_page.status_code == status.HTTP_200_OK, grouped_page.json()
    assert grouped_page.json()["total"] == 2

    attention_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "query": "Jira",
            "source_type": "third_party",
            "status_group": "needs_attention",
        },
    )
    assert attention_page.status_code == status.HTTP_200_OK, attention_page.json()
    assert attention_page.json()["total"] == 1
    assert attention_page.json()["items"][0]["canonical_app_slug"] == "jira"
    assert attention_page.json()["items"][0]["connection_status"] == "missing_scope"

    not_connected_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "source_type": "third_party",
            "status_group": "not_connected",
            "limit": 1,
            "offset": 1,
        },
    )
    assert (
        not_connected_page.status_code == status.HTTP_200_OK
    ), not_connected_page.json()
    assert not_connected_page.json()["total"] == 2
    assert not_connected_page.json()["items"][0]["canonical_app_slug"] == "dropbox"
    assert not_connected_page.json()["items"][0]["connection_status"] == "not_connected"

    facets = connected_body["facets"]
    assert facets["total"] == 6
    assert facets["source_type"] == {"native": 0, "third_party": 6}
    assert facets["status"]["connected"] == 1
    assert facets["status"]["configured"] == 1
    assert facets["status"]["pending"] == 1
    assert facets["status"]["missing_scope"] == 1
    assert facets["status"]["not_connected"] == 2
    assert facets["status_group"] == {
        "connected": 2,
        "needs_attention": 2,
        "not_connected": 2,
    }

    summary_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "status_group": "connected",
            "detail_level": "summary",
            "limit": 1,
        },
    )
    assert summary_page.status_code == status.HTTP_200_OK, summary_page.json()
    summary_item = summary_page.json()["items"][0]
    assert summary_item["available_actions"] == []
    assert summary_item["available_scopes"] == []
    assert summary_item["tool_count"] == 1
    assert summary_item["connection_status"] in {"connected", "configured"}

    invalid_status = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={**_owner_payload(assistant_id=assistant_id), "status": "bogus"},
    )
    assert invalid_status.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    invalid_group = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={**_owner_payload(assistant_id=assistant_id), "status_group": "bogus"},
    )
    assert invalid_group.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_post_read_integration_routes_are_removed(client: AsyncClient) -> None:
    removed_routes = [
        ("/v0/integrations/apps/get", {"limit": 1}),
        ("/v0/integrations/apps/search", {"query": "Slack"}),
        ("/v0/integrations/tools/get", {"limit": 1}),
        ("/v0/integrations/tools/search", {"query": "Slack"}),
    ]

    for path, payload in removed_routes:
        response = await client.post(path, headers=HEADERS, json=payload)
        assert response.status_code in {
            status.HTTP_404_NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        }, (path, response.status_code, response.text)


@pytest.mark.anyio
async def test_connection_tool_pagination_run_policy_and_audit(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    assistant_id = 77_000 + (uuid.uuid4().int % 1000)
    await _sync_integrations(
        client,
        app_slug="hubspot",
        display_name="HubSpot",
        tool_name="search_contacts",
        tool_display_name="Search HubSpot contacts",
        required_scopes=["crm.objects.contacts.read"],
    )

    start_response = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "hubspot",
            "backend_id": "composio",
            "requested_scopes": ["crm.objects.contacts.read"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert start_response.status_code == status.HTTP_200_OK, start_response.json()
    connection_id = start_response.json()["connection"]["connection_id"]

    tools = await client.get(
        "/v0/integrations/tools",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "hubspot",
            "activation_state": "connected_ready",
            "limit": 1,
            "offset": 0,
        },
    )
    assert tools.status_code == status.HTTP_200_OK, tools.json()
    assert tools.json()["total"] == 1
    tool = tools.json()["items"][0]
    assert tool["activation_state"] == "connected_ready"
    assert "input_schema" not in tool

    tools_with_schema = await client.get(
        "/v0/integrations/tools",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "hubspot",
            "activation_state": "connected_ready",
            "include_schema": True,
            "limit": 1,
            "offset": 0,
        },
    )
    assert tools_with_schema.status_code == status.HTTP_200_OK, tools_with_schema.json()
    tool_with_schema = tools_with_schema.json()["items"][0]
    assert tool_with_schema["input_schema"]["properties"]["query"]["type"] == "string"
    assert tool_with_schema["output_schema"] == {"type": "object"}

    schema = await client.get(
        f"/v0/integrations/tools/{tool['tool_id']}/schema?{_owner_query(assistant_id)}",
        headers=HEADERS,
    )
    assert schema.status_code == status.HTTP_200_OK, schema.json()
    assert schema.json()["canonical_name"] == tool["canonical_name"]

    run = await client.post(
        f"/v0/integrations/tools/{tool['tool_id']}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "connection_id": connection_id,
            "arguments": {"query": "alice"},
        },
    )
    assert run.status_code == status.HTTP_200_OK, run.json()
    assert run.json()["status"] == "ok"
    assert (
        dbsession.query(ProviderActionAudit)
        .filter_by(
            connection_id=connection_id,
            provider_tool_id="hubspot.search_contacts",
        )
        .count()
        == 1
    )

    policy = await client.patch(
        f"/v0/integrations/connections/{connection_id}/tool-policy",
        headers=HEADERS,
        json={"tool_policies": {tool["tool_id"]: "forbidden"}},
    )
    assert policy.status_code == status.HTTP_200_OK, policy.json()
    patched_policy = next(
        item for item in policy.json()["policies"] if item["tool_id"] == tool["tool_id"]
    )
    assert patched_policy["approval_level"] == "forbidden"

    blocked = await client.post(
        f"/v0/integrations/tools/{tool['tool_id']}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "connection_id": connection_id,
            "arguments": {"query": "alice"},
        },
    )
    assert blocked.status_code == status.HTTP_200_OK, blocked.json()
    assert blocked.json()["status"] == "blocked_by_policy"
    assert blocked.json()["error"]["code"] == "action_disabled_for_connection"


@pytest.mark.anyio
async def test_run_tool_confirmation_envelope(client: AsyncClient) -> None:
    assistant_id = 88_000 + (uuid.uuid4().int % 1000)
    await _sync_integrations(
        client,
        app_slug="slack",
        display_name="Slack",
        tool_name="send_message",
        tool_display_name="Send Slack message",
        action_class="write",
        required_scopes=["chat:write"],
    )
    start_response = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "slack",
            "backend_id": "composio",
            "requested_scopes": ["chat:write"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert start_response.status_code == status.HTTP_200_OK, start_response.json()
    connection_id = start_response.json()["connection"]["connection_id"]

    tools = await client.get(
        "/v0/integrations/tools/search",
        headers=HEADERS,
        params={
            **_owner_payload(assistant_id=assistant_id),
            "query": "Slack message",
            "include_unconnected": True,
        },
    )
    assert tools.status_code == status.HTTP_200_OK, tools.json()
    tool_id = tools.json()[0]["tool_id"]

    missing_confirmation = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "connection_id": connection_id,
            "arguments": {"channel": "general", "text": "hello"},
        },
    )
    assert (
        missing_confirmation.status_code == status.HTTP_200_OK
    ), missing_confirmation.json()
    assert missing_confirmation.json()["status"] == "confirmation_required"

    valid_token = create_confirmation_token(
        tool_id=tool_id,
        connection_id=connection_id,
    )
    confirmed = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "connection_id": connection_id,
            "confirmation_token": valid_token,
            "arguments": {"channel": "general", "text": "hello"},
        },
    )
    assert confirmed.status_code == status.HTTP_200_OK, confirmed.json()
    assert confirmed.json()["status"] == "ok"
