"""API-first tests for the provider-neutral integration control plane."""

from __future__ import annotations

import uuid

import pytest
from fastapi import status
from httpx import AsyncClient
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


async def _sync_catalog(
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
                        "required_scopes": required_scopes or ["read"],
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
    sync_response = await _sync_catalog(
        client,
        backend_id="pipedream",
        app_slug="linear",
        display_name="Linear",
        tool_name="list_issues",
        tool_display_name="List Linear issues",
    )
    assert sync_response == {"apps_upserted": 1, "tools_upserted": 1}


@pytest.mark.anyio
async def test_backend_status_is_the_only_catalog_visibility_gate(
    client: AsyncClient,
) -> None:
    await _sync_catalog(
        client,
        backend_id="pipedream",
        app_slug="linear",
        display_name="Linear",
        tool_name="list_issues",
        tool_display_name="List Linear issues",
    )

    hidden = await client.post(
        "/v0/integrations/apps/search",
        headers=HEADERS,
        json={"query": "Linear"},
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

    visible = await client.post(
        "/v0/integrations/apps/search",
        headers=HEADERS,
        json={"query": "Linear"},
    )
    assert visible.status_code == status.HTTP_200_OK, visible.json()
    assert visible.json()[0]["canonical_app_slug"] == "linear"


@pytest.mark.anyio
async def test_native_app_sync_search_and_connection_rejection(
    client: AsyncClient,
) -> None:
    await _sync_catalog(
        client,
        backend_id="unity_native",
        app_slug="matterport",
        display_name="Matterport",
        source_type="native",
    )

    page = await client.post(
        "/v0/integrations/apps/get",
        headers=HEADERS,
        json={"source_type": "native", "query": "Matterport"},
    )
    assert page.status_code == status.HTTP_200_OK, page.json()
    assert page.json()["total"] == 1
    assert page.json()["items"][0]["source_label"] == "Native"
    assert page.json()["items"][0]["native_metadata"]["tier"] == "api"

    third_party_page = await client.post(
        "/v0/integrations/apps/get",
        headers=HEADERS,
        json={"source_type": "third_party", "query": "Matterport"},
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
async def test_connection_tool_pagination_run_policy_and_audit(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    assistant_id = 77_000 + (uuid.uuid4().int % 1000)
    await _sync_catalog(
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

    tools = await client.post(
        "/v0/integrations/tools/get",
        headers=HEADERS,
        json={
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
    await _sync_catalog(
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

    tools = await client.post(
        "/v0/integrations/tools/search",
        headers=HEADERS,
        json={
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
