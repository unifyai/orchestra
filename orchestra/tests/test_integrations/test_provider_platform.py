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
    IntegrationBackend,
    ProviderActionAudit,
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


def _tool_metadata(
    *,
    app_slug: str,
    tool_name: str,
    display_name: str,
    action_class: str = "read",
    required_scopes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "backend_id": "composio",
        "provider_app_id": app_slug,
        "canonical_app_slug": app_slug,
        "app_display_name": app_slug.replace("_", " ").title(),
        "provider_tool_id": f"{app_slug}.{tool_name}",
        "canonical_name": f"primitives.integrations.{app_slug}.{tool_name}",
        "function_manager_name": f"primitives_integrations__{app_slug}__{tool_name}",
        "tool_display_name": display_name,
        "action_class": action_class,
        "required_scopes": required_scopes or [],
        "behavior_hints": ["mutates_state"] if action_class == "write" else [],
        "confirmation_required": action_class
        in {"write", "destructive", "bulk_export"},
    }


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


def test_provider_backend_unique_constraints(dbsession: Session) -> None:
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


def test_cloud_bootstrap_passes_prune_unlisted_apps_to_sync() -> None:
    manifest = {
        "schema_version": 1,
        "environment": "selfhost",
        "providers": {
            "composio": {
                "status": "enabled",
                "sync": {
                    "mode": "partial",
                    "app_slugs": ["GMAIL"],
                    "prune_unlisted_apps": True,
                },
            },
        },
    }
    plan = provider_plans(manifest)[0]

    class FakeClient:
        def __init__(self) -> None:
            self.sync_payloads: list[dict] = []
            self.state_updates: list[dict] = []

        def request(self, method: str, path: str, payload=None):
            if path == "/admin/integrations/backends":
                return {}
            assert path == "/admin/integrations/sync"
            self.sync_payloads.append(dict(payload))
            return {
                "status": "success",
                "apps_upserted": 1,
                "tools_upserted": 1,
                "apps": [{"canonical_app_slug": "gmail"}],
                "tools": [
                    {"canonical_app_slug": "gmail", "provider_tool_id": "gmail.search"},
                ],
                "matched_app_slugs": ["gmail"],
                "cache_version": payload["cache_version"],
            }

        def bootstrap_state(self, *, environment: str, backend_id: str):
            return None

        def put_bootstrap_state(
            self,
            *,
            environment,
            plan,
            status,
            result=None,
            error_message=None,
        ):
            self.state_updates.append({"status": status, "result": result})

    client = FakeClient()
    result = apply_plan(
        client=client,
        environment="selfhost",
        plan=plan,
    )

    assert result == "synced"
    assert client.sync_payloads[0]["prune_unlisted_apps"] is True
    diagnostics = _sync_diagnostics(plan=plan, result=client.state_updates[0]["result"])
    assert diagnostics["prune_unlisted_apps"] is True


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
    tombstone = await client.get(
        "/v0/integrations/apps/search",
        headers=HEADERS,
        params={"query": "Linear"},
    )
    assert tombstone.status_code == status.HTTP_410_GONE
    assert "Builtins logs" in tombstone.json()["detail"]

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

    still_tombstoned = await client.get(
        "/v0/integrations/apps/search",
        headers=HEADERS,
        params={"query": "Linear"},
    )
    assert still_tombstoned.status_code == status.HTTP_410_GONE
    assert "Builtins logs" in still_tombstoned.json()["detail"]


@pytest.mark.anyio
async def test_native_app_sync_search_and_connection_rejection(
    client: AsyncClient,
) -> None:
    page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={"source_type": "native", "query": "Matterport"},
    )
    assert page.status_code == status.HTTP_410_GONE
    assert "Builtins logs" in page.json()["detail"]

    third_party_page = await client.get(
        "/v0/integrations/apps",
        headers=HEADERS,
        params={"source_type": "third_party", "query": "Matterport"},
    )
    assert third_party_page.status_code == status.HTTP_410_GONE
    assert "Builtins logs" in third_party_page.json()["detail"]

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
    slack_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_slack",
            "backend_id": "composio",
            "provider_app_id": "scope_slack",
            "requested_scopes": ["chat:write"],
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
    assert connected_tools.status_code == status.HTTP_410_GONE, connected_tools.text

    mail_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_mail",
            "backend_id": "composio",
            "provider_app_id": "scope_mail",
            "requested_scopes": [
                "https://mail.google.com/",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
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
            "provider_app_id": "scope_mail",
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
            "provider_app_id": "scope_free",
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
            "provider_app_id": "scope_mail",
            "requested_scopes": [
                "https://mail.google.com/",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
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
    assert api_key_tools.status_code == status.HTTP_410_GONE, api_key_tools.text

    api_key_scope_free_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "scope_free",
            "backend_id": "composio",
            "provider_app_id": "scope_free",
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
async def test_connection_start_reuses_connection_id_on_reconnect(
    client: AsyncClient,
) -> None:
    assistant_id = 121_000 + (uuid.uuid4().int % 1000)
    owner = _owner_payload(assistant_id=assistant_id)

    first_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **owner,
            "canonical_app_slug": "reconnect_app",
            "backend_id": "composio",
            "provider_app_id": "reconnect_app",
            "requested_scopes": [],
            "auth_mode": "oauth",
        },
    )
    assert first_start.status_code == status.HTTP_200_OK, first_start.json()
    connection_id = first_start.json()["connection"]["connection_id"]

    complete = await client.post(
        f"/v0/integrations/connections/{connection_id}/complete",
        headers=HEADERS,
        json={
            "provider_connection_id": "provider-reconnect-app",
            "granted_scopes": [],
            "status": "connected",
        },
    )
    assert complete.status_code == status.HTTP_200_OK, complete.json()

    disconnect = await client.post(
        f"/v0/integrations/connections/{connection_id}/disconnect",
        headers=HEADERS,
    )
    assert disconnect.status_code == status.HTTP_200_OK, disconnect.json()
    assert disconnect.json()["status"] == "disconnected"

    second_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **owner,
            "canonical_app_slug": "reconnect_app",
            "backend_id": "composio",
            "provider_app_id": "reconnect_app",
            "requested_scopes": [],
            "auth_mode": "oauth",
        },
    )
    assert second_start.status_code == status.HTTP_200_OK, second_start.json()
    assert second_start.json()["connection"]["connection_id"] == connection_id
    assert second_start.json()["connection"]["status"] == "pending"

    connections = await client.get(
        "/v0/integrations/connections",
        headers=HEADERS,
        params={**owner, "include_disconnected": True},
    )
    assert connections.status_code == status.HTTP_200_OK, connections.text
    matching = [
        conn
        for conn in connections.json()
        if conn["canonical_app_slug"] == "reconnect_app"
    ]
    assert len(matching) == 1
    assert matching[0]["connection_id"] == connection_id


@pytest.mark.anyio
async def test_connection_start_reuses_connection_id_on_same_account_double_submit(
    client: AsyncClient,
) -> None:
    """A double-submit of the same account while already connected (double-
    click, UI retry) must reuse the existing row, not mint a duplicate.
    """
    assistant_id = 121_000 + (uuid.uuid4().int % 1000)
    owner = _owner_payload(assistant_id=assistant_id)
    payload = {
        **owner,
        "canonical_app_slug": "double_submit_app",
        "backend_id": "composio",
        "provider_app_id": "double_submit_app",
        "requested_scopes": ["tasks:write"],
        "auth_mode": "api_key",
        "api_key_fields": {"token": "secret"},
    }

    first_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json=payload,
    )
    assert first_start.status_code == status.HTTP_200_OK, first_start.json()
    first_connection = first_start.json()["connection"]
    assert first_connection["status"] == "connected"

    second_start = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json=payload,
    )
    assert second_start.status_code == status.HTTP_200_OK, second_start.json()
    assert (
        second_start.json()["connection"]["connection_id"]
        == first_connection["connection_id"]
    )

    connections = await client.get(
        "/v0/integrations/connections",
        headers=HEADERS,
        params={**owner, "include_disconnected": True},
    )
    assert connections.status_code == status.HTTP_200_OK, connections.text
    matching = [
        conn
        for conn in connections.json()
        if conn["canonical_app_slug"] == "double_submit_app"
    ]
    assert len(matching) == 1


@pytest.mark.anyio
async def test_app_catalog_status_filters_facets_and_summary_payload(
    client: AsyncClient,
) -> None:
    for path in (
        "/v0/integrations/apps",
        "/v0/integrations/apps/search",
        "/v0/integrations/tools",
        "/v0/integrations/tools/search",
        "/v0/integrations/tools/composio:slack:send_message/schema",
    ):
        response = await client.get(path, headers=HEADERS)
        assert response.status_code == status.HTTP_410_GONE, (path, response.text)
        assert "Builtins logs" in response.json()["detail"]


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
    tool_id = "composio:hubspot:search_contacts"
    tool_metadata = _tool_metadata(
        app_slug="hubspot",
        tool_name="search_contacts",
        display_name="Search HubSpot contacts",
        required_scopes=["crm.objects.contacts.read"],
    )

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
    assert tools.status_code == status.HTTP_410_GONE, tools.text

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
    assert tools_with_schema.status_code == status.HTTP_410_GONE, tools_with_schema.text

    schema = await client.get(
        f"/v0/integrations/tools/{tool_id}/schema?{_owner_query(assistant_id)}",
        headers=HEADERS,
    )
    assert schema.status_code == status.HTTP_410_GONE, schema.text

    run = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
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
        json={"tool_policies": {tool_id: "forbidden"}},
    )
    assert policy.status_code == status.HTTP_200_OK, policy.json()
    patched_policy = next(
        item for item in policy.json()["policies"] if item["tool_id"] == tool_id
    )
    assert patched_policy["approval_level"] == "forbidden"

    blocked = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"query": "alice"},
        },
    )
    assert blocked.status_code == status.HTTP_200_OK, blocked.json()
    assert blocked.json()["status"] == "blocked_by_policy"
    assert blocked.json()["error"]["code"] == "action_disabled_for_connection"


@pytest.mark.anyio
async def test_run_tool_uses_owner_external_user_id_when_body_user_missing(
    client: AsyncClient,
) -> None:
    assistant_id = 78_000 + (uuid.uuid4().int % 1000)
    start_response = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            "owner_scope": "assistant",
            "assistant_id": assistant_id,
            "canonical_app_slug": "gmail",
            "backend_id": "composio",
            "provider_app_id": "gmail",
            "requested_scopes": ["https://www.googleapis.com/auth/gmail.labels"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert start_response.status_code == status.HTTP_200_OK, start_response.json()
    connection_id = start_response.json()["connection"]["connection_id"]
    tool_id = "composio:gmail:get_labels"
    tool_metadata = _tool_metadata(
        app_slug="gmail",
        tool_name="get_labels",
        display_name="List Gmail labels",
        required_scopes=["https://www.googleapis.com/auth/gmail.labels"],
    )

    run = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            "owner_scope": "assistant",
            "assistant_id": assistant_id,
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"user_id": "me"},
        },
    )
    assert run.status_code == status.HTTP_200_OK, run.json()
    assert run.json()["status"] == "ok"
    assert run.json()["result"]["user_id"] == f"assistant:{assistant_id}"


@pytest.mark.anyio
async def test_run_tool_executes_when_connection_holds_superscope(
    client: AsyncClient,
) -> None:
    """A connection holding only a broad superscope must not be blocked.

    The tool declares narrower scope variants the connection does not literally
    hold; a local subset prediction would wrongly block it. Authorization is the
    provider's job, so the run must reach the adapter and succeed.
    """

    assistant_id = 79_000 + (uuid.uuid4().int % 1000)
    start_response = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "gmail",
            "backend_id": "composio",
            "provider_app_id": "gmail",
            "requested_scopes": ["https://mail.google.com/"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert start_response.status_code == status.HTTP_200_OK, start_response.json()
    connection_id = start_response.json()["connection"]["connection_id"]
    granted = start_response.json()["connection"]["granted_scopes"]
    assert granted == ["https://mail.google.com/"]

    tool_metadata = _tool_metadata(
        app_slug="gmail",
        tool_name="fetch_emails",
        display_name="Fetch Gmail emails",
        required_scopes=[
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
    )

    run = await client.post(
        "/v0/integrations/tools/composio:gmail:fetch_emails/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"query": "is:unread"},
        },
    )
    assert run.status_code == status.HTTP_200_OK, run.json()
    assert run.json()["status"] == "ok"
    assert run.json()["activation_state"] == "connected_ready"


@pytest.mark.anyio
async def test_run_tool_maps_provider_403_to_missing_scope(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider 403 (insufficient permissions) is surfaced as missing_scope
    using the provider's own response, not a local scope comparison."""

    from orchestra.integrations.providers.base import ProviderExecutionResult
    from orchestra.web.api.integrations import operations as ops

    assistant_id = 79_500 + (uuid.uuid4().int % 1000)
    start_response = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "gmail",
            "backend_id": "composio",
            "provider_app_id": "gmail",
            "requested_scopes": ["https://mail.google.com/"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert start_response.status_code == status.HTTP_200_OK, start_response.json()
    connection_id = start_response.json()["connection"]["connection_id"]

    class _ForbiddenAdapter:
        def execute(self, request: object) -> ProviderExecutionResult:
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_request_failed",
                    "message": "403 Client Error",
                    "provider_status_code": 403,
                    "provider_response_body": "insufficient authentication scopes",
                },
            )

    monkeypatch.setattr(
        ops,
        "get_provider_adapter",
        lambda *a, **k: _ForbiddenAdapter(),
    )

    tool_metadata = _tool_metadata(
        app_slug="gmail",
        tool_name="fetch_emails",
        display_name="Fetch Gmail emails",
        required_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    run = await client.post(
        "/v0/integrations/tools/composio:gmail:fetch_emails/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"query": "is:unread"},
        },
    )
    assert run.status_code == status.HTTP_200_OK, run.json()
    body = run.json()
    assert body["status"] == "missing_scope"
    assert body["activation_state"] == "missing_scope"
    assert body["error"]["code"] == "missing_scope"
    assert body["error"]["required_scopes"] == [
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    assert (
        body["error"]["provider_response_body"] == "insufficient authentication scopes"
    )


@pytest.mark.anyio
async def test_run_tool_maps_provider_401_to_reconnect_required(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider 401 (unauthenticated token) is surfaced as a reconnect
    outcome derived from the provider response."""

    from orchestra.integrations.providers.base import ProviderExecutionResult
    from orchestra.web.api.integrations import operations as ops

    assistant_id = 80_000 + (uuid.uuid4().int % 1000)
    start_response = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "gmail",
            "backend_id": "composio",
            "provider_app_id": "gmail",
            "requested_scopes": ["https://mail.google.com/"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
        },
    )
    assert start_response.status_code == status.HTTP_200_OK, start_response.json()
    connection_id = start_response.json()["connection"]["connection_id"]

    class _UnauthenticatedAdapter:
        def execute(self, request: object) -> ProviderExecutionResult:
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_request_failed",
                    "message": "401 Client Error",
                    "provider_status_code": 401,
                    "provider_response_body": "invalid credentials",
                },
            )

    monkeypatch.setattr(
        ops,
        "get_provider_adapter",
        lambda *a, **k: _UnauthenticatedAdapter(),
    )

    tool_metadata = _tool_metadata(
        app_slug="gmail",
        tool_name="fetch_emails",
        display_name="Fetch Gmail emails",
        required_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    run = await client.post(
        "/v0/integrations/tools/composio:gmail:fetch_emails/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"query": "is:unread"},
        },
    )
    assert run.status_code == status.HTTP_200_OK, run.json()
    body = run.json()
    assert body["status"] == "reconnect_required"
    assert body["activation_state"] == "expired"
    assert body["error"]["code"] == "reconnect_required"
    assert body["error"]["provider_response_body"] == "invalid credentials"


@pytest.mark.anyio
async def test_run_tool_confirmation_envelope(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    assistant_id = 88_000 + (uuid.uuid4().int % 1000)
    start_response = await client.post(
        "/v0/integrations/connect/start",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            "canonical_app_slug": "slack",
            "backend_id": "composio",
            "provider_app_id": "slack",
            "requested_scopes": ["chat:write"],
            "auth_mode": "api_key",
            "api_key_fields": {"token": "secret"},
            "account_label": "Workspace Slack",
        },
    )
    assert start_response.status_code == status.HTTP_200_OK, start_response.json()
    connection_id = start_response.json()["connection"]["connection_id"]
    tool_id = "composio:slack:send_message"
    tool_metadata = _tool_metadata(
        app_slug="slack",
        tool_name="send_message",
        display_name="Send Slack message",
        action_class="write",
        required_scopes=["chat:write"],
    )

    missing_confirmation = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"channel": "general", "text": "hello"},
        },
    )
    assert (
        missing_confirmation.status_code == status.HTTP_200_OK
    ), missing_confirmation.json()
    missing_payload = missing_confirmation.json()
    assert missing_payload["status"] == "confirmation_required"
    confirmation = missing_payload["confirmation"]
    assert confirmation["audit_id"] == missing_payload["audit_id"]
    assert confirmation["connection_id"] == connection_id
    assert confirmation["tool_id"] == tool_id
    assert confirmation["app_slug"] == "slack"
    assert confirmation["app_display_name"] == "Slack"
    assert confirmation["account_label"] == "Workspace Slack"
    assert confirmation["tool_display_name"] == "Send Slack message"
    assert confirmation["action_class"] == "write"
    assert confirmation["behavior_hints"] == ["mutates_state"]
    assert confirmation["approval_level"] == "specific_approval"
    assert confirmation["arguments_summary"] == {
        "keys": ["channel", "text"],
        "total_keys": 2,
    }
    assert "once" in confirmation["approval_options"]
    pending_audit = dbsession.query(ProviderActionAudit).get(
        missing_payload["audit_id"],
    )
    assert pending_audit is not None
    assert pending_audit.status == "pending_confirmation"
    assert pending_audit.tool_id == tool_id
    assert pending_audit.arguments_hash

    valid_token = create_confirmation_token(
        tool_id=tool_id,
        connection_id=connection_id,
    )
    confirmed = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "confirmation_token": valid_token,
            "arguments": {"channel": "general", "text": "hello"},
        },
    )
    assert confirmed.status_code == status.HTTP_200_OK, confirmed.json()
    assert confirmed.json()["status"] == "ok"

    pending_once = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"channel": "general", "text": "approve once"},
        },
    )
    assert pending_once.status_code == status.HTTP_200_OK, pending_once.json()
    once_audit_id = pending_once.json()["audit_id"]
    wrong_owner_approval = await client.post(
        f"/v0/integrations/tool-executions/{once_audit_id}/approve",
        headers=HEADERS,
        json={
            "owner_scope": "assistant",
            "assistant_id": assistant_id + 1,
            "scope": "once",
            "actor_id": "user:test",
        },
    )
    assert wrong_owner_approval.status_code == status.HTTP_403_FORBIDDEN

    approval = await client.post(
        f"/v0/integrations/tool-executions/{once_audit_id}/approve",
        headers=HEADERS,
        json={
            "owner_scope": "assistant",
            "assistant_id": assistant_id,
            "scope": "once",
            "actor_id": "user:test",
        },
    )
    assert approval.status_code == status.HTTP_200_OK, approval.json()
    assert approval.json()["status"] == "approved"
    assert approval.json()["audit_id"] == once_audit_id
    assert approval.json()["connection_id"] == connection_id
    assert approval.json()["tool_id"] == tool_id
    assert approval.json()["approval_scope"] == "once"
    assert approval.json()["approval_level"] == "auto"
    assert approval.json()["confirmation_token"]
    assert approval.json()["expires_at"]
    assert approval.json()["policy_updated"] is False

    approved_retry = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "approval_audit_id": once_audit_id,
            "arguments": {"channel": "general", "text": "approve once"},
        },
    )
    assert approved_retry.status_code == status.HTTP_200_OK, approved_retry.json()
    assert approved_retry.json()["status"] == "ok"
    dbsession.expire_all()
    approved_audit = dbsession.query(ProviderActionAudit).get(once_audit_id)
    assert approved_audit.status == "ok"
    assert approved_audit.approved_by == "user:test"

    pending_deny = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"channel": "general", "text": "deny"},
        },
    )
    deny_audit_id = pending_deny.json()["audit_id"]
    denial = await client.post(
        f"/v0/integrations/tool-executions/{deny_audit_id}/deny",
        headers=HEADERS,
        json={
            "owner_scope": "assistant",
            "assistant_id": assistant_id,
            "scope": "once",
            "actor_id": "user:test",
            "reason": "no",
        },
    )
    assert denial.status_code == status.HTTP_200_OK, denial.json()
    assert denial.json()["status"] == "denied"
    assert denial.json()["audit_id"] == deny_audit_id
    assert denial.json()["connection_id"] == connection_id
    assert denial.json()["tool_id"] == tool_id
    assert denial.json()["approval_scope"] == "once"
    assert denial.json()["approval_level"] == "forbidden"
    assert denial.json()["confirmation_token"] is None
    denied_retry = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "approval_audit_id": deny_audit_id,
            "arguments": {"channel": "general", "text": "deny"},
        },
    )
    assert denied_retry.status_code == status.HTTP_200_OK, denied_retry.json()
    assert denied_retry.json()["status"] == "confirmation_required"
    assert denied_retry.json()["error"]["code"] == "invalid_approval"

    pending_persist = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"channel": "general", "text": "persist"},
        },
    )
    persist_audit_id = pending_persist.json()["audit_id"]
    persisted = await client.post(
        f"/v0/integrations/tool-executions/{persist_audit_id}/approve",
        headers=HEADERS,
        json={
            "scope": "tool",
            "persist_policy": True,
            "approval_level": "auto",
            "actor_id": "user:test",
        },
    )
    assert persisted.status_code == status.HTTP_200_OK, persisted.json()
    assert persisted.json()["policy_updated"] is True
    auto_allowed = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_id,
            "arguments": {"channel": "general", "text": "future"},
        },
    )
    assert auto_allowed.status_code == status.HTTP_200_OK, auto_allowed.json()
    assert auto_allowed.json()["status"] == "ok"


@pytest.mark.anyio
async def test_tool_policy_is_scoped_to_connection_account(
    client: AsyncClient,
) -> None:
    assistant_id = 89_000 + (uuid.uuid4().int % 1000)
    tool_id = "composio:asana:create_task"
    read_tool_id = "composio:asana:list_tasks"
    tool_metadata = _tool_metadata(
        app_slug="asana",
        tool_name="create_task",
        display_name="Create Asana task",
        action_class="write",
        required_scopes=["tasks:write"],
    )
    connection_ids: list[str] = []
    for label in ["Work Asana", "Personal Asana"]:
        response = await client.post(
            "/v0/integrations/connect/start",
            headers=HEADERS,
            json={
                **_owner_payload(assistant_id=assistant_id),
                "canonical_app_slug": "asana",
                "backend_id": "composio",
                "provider_app_id": "asana",
                "requested_scopes": ["tasks:write", "tasks:read"],
                "auth_mode": "api_key",
                "api_key_fields": {"token": "secret"},
                "account_label": label,
            },
        )
        assert response.status_code == status.HTTP_200_OK, response.json()
        connection_ids.append(response.json()["connection"]["connection_id"])

    policy_before = await client.get(
        f"/v0/integrations/connections/{connection_ids[0]}/tool-policy",
        headers=HEADERS,
        params={"owner_scope": "assistant", "assistant_id": assistant_id},
    )
    assert policy_before.status_code == status.HTTP_200_OK, policy_before.json()
    assert policy_before.json()["account_label"] == "Work Asana"
    assert policy_before.json()["app_display_name"] == "Asana"
    assert policy_before.json()["policies"] == []

    patched = await client.patch(
        f"/v0/integrations/connections/{connection_ids[0]}/tool-policy",
        headers=HEADERS,
        params={"owner_scope": "assistant", "assistant_id": assistant_id},
        json={"tool_policies": {tool_id: "auto"}},
    )
    assert patched.status_code == status.HTTP_200_OK, patched.json()
    patched_by_id = {item["tool_id"]: item for item in patched.json()["policies"]}
    assert patched_by_id[tool_id]["approval_level"] == "auto"

    second_patch = await client.patch(
        f"/v0/integrations/connections/{connection_ids[0]}/tool-policy",
        headers=HEADERS,
        params={"owner_scope": "assistant", "assistant_id": assistant_id},
        json={"tool_policies": {read_tool_id: "forbidden"}},
    )
    assert second_patch.status_code == status.HTTP_200_OK, second_patch.json()
    second_by_id = {item["tool_id"]: item for item in second_patch.json()["policies"]}
    assert second_by_id[tool_id]["approval_level"] == "auto"
    assert second_by_id[read_tool_id]["approval_level"] == "forbidden"

    forbidden_owner = await client.get(
        f"/v0/integrations/connections/{connection_ids[0]}/tool-policy",
        headers=HEADERS,
        params={"owner_scope": "assistant", "assistant_id": assistant_id + 1},
    )
    assert forbidden_owner.status_code == status.HTTP_403_FORBIDDEN

    other_policy = await client.get(
        f"/v0/integrations/connections/{connection_ids[1]}/tool-policy",
        headers=HEADERS,
        params={"owner_scope": "assistant", "assistant_id": assistant_id},
    )
    assert other_policy.status_code == status.HTTP_200_OK, other_policy.json()
    assert other_policy.json()["policies"] == []

    auto_run = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_ids[0],
            "arguments": {"name": "ship account scoped policy"},
        },
    )
    assert auto_run.status_code == status.HTTP_200_OK, auto_run.json()
    assert auto_run.json()["status"] == "ok"

    confirm_run = await client.post(
        f"/v0/integrations/tools/{tool_id}/run",
        headers=HEADERS,
        json={
            **_owner_payload(assistant_id=assistant_id),
            **tool_metadata,
            "connection_id": connection_ids[1],
            "arguments": {"name": "ask on other account"},
        },
    )
    assert confirm_run.status_code == status.HTTP_200_OK, confirm_run.json()
    assert confirm_run.json()["status"] == "confirmation_required"
