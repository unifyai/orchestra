"""API-local integration operations.

The public integration contract is the HTTP API in ``views.py``. This module is
kept beside those routes to hold provider orchestration that is too large to
inline in route functions: catalog sync normalization, provider connect URL
construction, policy checks, semantic catalog scoring, and execution auditing.
Database persistence goes through ``IntegrationProviderDAO`` and external calls
go through provider adapters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy.orm import Session

from orchestra.artifacts.embedding_runtime import (
    CATALOG_SEARCH_KEY,
    ArtifactEmbeddingRuntime,
)
from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.models.integration_provider_models import (
    DynamicProviderApp,
    IntegrationBackend,
    IntegrationConnection,
    IntegrationOverlay,
    ProviderToolCatalog,
)
from orchestra.integrations.providers import (
    ProviderExecutionRequest,
    get_provider_adapter,
)
from orchestra.web.api.integrations.schema import (
    DynamicIntegrationAppResponse,
    IntegrationAppDetailResponse,
    IntegrationCatalogSyncRequest,
    IntegrationCatalogSyncResponse,
    IntegrationConnectionResponse,
    IntegrationToolPolicyItem,
    IntegrationToolPolicyPatchRequest,
    IntegrationToolPolicyResponse,
    ProviderAppGetRequest,
    ProviderAppGetResponse,
    ProviderAppSearchRequest,
    ProviderAppSearchResult,
    ProviderToolGetRequest,
    ProviderToolGetResponse,
    ProviderToolRunRequest,
    ProviderToolRunResponse,
    ProviderToolSchemaResponse,
    ProviderToolSearchRequest,
    ProviderToolSearchResult,
)

READY_STATUSES = {"connected"}
EXPIRED_STATUSES = {"expired", "revoked", "error"}
SemanticToolScoreProvider = Callable[
    [Session, str, list[ProviderToolCatalog]],
    dict[str, float | tuple[float, str]],
]
_SEMANTIC_TOOL_SCORE_PROVIDER: Optional[SemanticToolScoreProvider] = None
CATALOG_ARTIFACT_EMBEDDINGS = ArtifactEmbeddingRuntime(key=CATALOG_SEARCH_KEY)
INTEGRATION_APP_EMBEDDING_NAMESPACE = "integration_app"
INTEGRATION_TOOL_EMBEDDING_NAMESPACE = "integration_tool"
GLOBAL_CATALOG_SEMANTIC_SCORE_CUTOFF = 0.35
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerContext:
    owner_scope: str = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None


DEFAULT_BACKENDS = [
    {
        "backend_id": "composio",
        "kind": "composio",
        "environment": "prod",
        "display_name": "Composio",
        "status": "enabled",
        "default_priority": 10,
    },
    {
        "backend_id": "pipedream",
        "kind": "pipedream",
        "environment": "prod",
        "display_name": "Pipedream",
        "status": "disabled",
        "default_priority": 20,
    },
    {
        "backend_id": "unity_native",
        "kind": "first_party",
        "environment": "prod",
        "display_name": "Unity Native",
        "status": "enabled",
        "default_priority": 1,
        "config_json": {"execution_mode": "function_manager"},
    },
]


def seed_default_provider_catalog(session: Session) -> None:
    """Ensure configurable integration backend rows exist.

    Only backend rows are bootstrapped here. Apps/tools are provider or native
    catalog data and must be synced through admin API endpoints, so deployments
    never get hidden local sample apps/tools or app allowlists. Existing backend
    rows are not overwritten; operators enable/disable providers here, while
    provider credentials/endpoints come from deployment environment variables.
    """

    IntegrationProviderDAO(session).seed_default_backends(
        default_backends=DEFAULT_BACKENDS,
    )
    session.commit()


def _slugify(value: str) -> str:
    normalized = "".join(
        char.lower() if char.isalnum() else "_" for char in value.strip()
    )
    return "_".join(part for part in normalized.split("_") if part)


_COMPOSIO_APP_SLUG_OVERRIDES = {
    "GOOGLEDRIVE": "google_drive",
    "GOOGLECALENDAR": "google_calendar",
    "GOOGLEDOCS": "google_docs",
}


def _composio_canonical_app_slug(provider_app_id: str) -> str:
    normalized = provider_app_id.strip().upper()
    return _COMPOSIO_APP_SLUG_OVERRIDES.get(normalized, _slugify(normalized))


def _composio_tool_name(provider_tool_id: str, provider_app_id: str) -> str:
    normalized_tool = provider_tool_id.strip().upper()
    normalized_app = provider_app_id.strip().upper()
    for prefix in (f"{normalized_app}_", f"{normalized_app}."):
        if normalized_tool.startswith(prefix):
            return _slugify(normalized_tool[len(prefix) :])
    return _slugify(normalized_tool)


def _composio_auth_modes(toolkit: dict[str, Any]) -> list[str]:
    schemes = (
        toolkit.get("auth_schemes")
        or toolkit.get("authSchemes")
        or toolkit.get("auth")
        or []
    )
    if isinstance(schemes, str):
        schemes = [schemes]
    modes: list[str] = []
    for scheme in schemes:
        normalized = str(scheme).upper()
        if "OAUTH" in normalized:
            modes.append("oauth")
        elif "API" in normalized or "TOKEN" in normalized or "KEY" in normalized:
            modes.append("api_key")
        elif "NO_AUTH" in normalized:
            modes.append("custom")
    return modes or ["oauth"]


def _composio_icon_url(toolkit: dict[str, Any]) -> str | None:
    meta = toolkit.get("meta") if isinstance(toolkit.get("meta"), dict) else {}
    for value in (
        toolkit.get("logo"),
        toolkit.get("icon_url"),
        toolkit.get("iconUrl"),
        meta.get("logo"),
        meta.get("icon_url"),
        meta.get("iconUrl"),
    ):
        if value:
            return str(value)
    return None


def _composio_toolkit_slug(tool: dict[str, Any]) -> str | None:
    toolkit = tool.get("toolkit")
    if isinstance(toolkit, dict):
        slug = toolkit.get("slug")
        if slug:
            return str(slug)
    return None


def _composio_tool_scopes(tool: dict[str, Any]) -> list[str]:
    scopes = tool.get("scopes") or []
    if isinstance(scopes, dict):
        scopes = list(scopes.keys())
    if not isinstance(scopes, list):
        return []
    return [str(scope) for scope in scopes if scope]


def _normalize_account_label(value: Optional[str]) -> Optional[str]:
    return (value or "").strip() or None


def _composio_action_class(tool: dict[str, Any], canonical_app_slug: str) -> str:
    return _provider_action_class(
        provider_tool_id=str(tool.get("slug") or ""),
        name=str(tool.get("name") or ""),
        description=str(tool.get("description") or ""),
        canonical_app_slug=canonical_app_slug,
    )


def _provider_action_class(
    *,
    provider_tool_id: str,
    name: str,
    description: str,
    canonical_app_slug: str,
) -> str:
    text = " ".join([provider_tool_id, name, description]).lower()
    if any(word in text for word in ["delete", "remove", "revoke", "destroy"]):
        return "destructive"
    if any(
        word in text
        for word in [
            "send",
            "create",
            "update",
            "write",
            "post",
            "upload",
            "invite",
            "add ",
        ]
    ):
        return "write"
    sensitive_apps = {
        "gmail",
        "google_drive",
        "google_docs",
        "one_drive",
        "share_point",
        "slack",
        "discord",
        "discordbot",
    }
    sensitive_terms = [
        "message",
        "history",
        "email",
        "file",
        "drive",
        "document",
        "content",
        "guild",
        "channel",
    ]
    if canonical_app_slug in sensitive_apps and any(
        term in text for term in sensitive_terms
    ):
        return "sensitive_read"
    return "read"


def _pipedream_app_slug(app: dict[str, Any]) -> str:
    value = (
        app.get("name_slug")
        or app.get("slug")
        or app.get("id")
        or app.get("name")
        or ""
    )
    return _slugify(str(value))


def _pipedream_tool_name(provider_tool_id: str, canonical_app_slug: str) -> str:
    normalized_tool = provider_tool_id.strip()
    for prefix in (
        f"{canonical_app_slug}-",
        f"{canonical_app_slug}_",
        f"{canonical_app_slug}.",
    ):
        if normalized_tool.startswith(prefix):
            return _slugify(normalized_tool[len(prefix) :])
    return _slugify(normalized_tool)


def _pipedream_category(app: dict[str, Any]) -> str | None:
    category = app.get("category")
    if isinstance(category, dict):
        return category.get("name") or category.get("slug")
    categories = app.get("categories")
    if isinstance(categories, list) and categories:
        first = categories[0]
        if isinstance(first, dict):
            return first.get("name") or first.get("slug")
        return str(first)
    return str(category) if category else None


def _pipedream_input_schema(component: dict[str, Any]) -> dict[str, Any]:
    props = component.get("props")
    if isinstance(props, dict):
        return {"type": "object", "properties": props}
    schema = component.get("input_schema") or component.get("inputSchema") or {}
    return schema if isinstance(schema, dict) else {"type": "object"}


def _composio_tool_input_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = (
        tool.get("input_parameters")
        or tool.get("inputParameters")
        or tool.get("input_schema")
        or tool.get("inputSchema")
        or {}
    )
    return schema if isinstance(schema, dict) else {"type": "object"}


def _composio_tool_output_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = (
        tool.get("output_parameters")
        or tool.get("outputParameters")
        or tool.get("output_schema")
        or tool.get("outputSchema")
        or {}
    )
    return schema if isinstance(schema, dict) else {"type": "object"}


def _confirmation_secret() -> bytes:
    return os.getenv(
        "INTEGRATION_CONFIRMATION_SECRET",
        "local-integration-confirmation-secret",
    ).encode()


def create_confirmation_token(
    *,
    tool_id: str,
    connection_id: str,
    ttl_seconds: int = 900,
) -> str:
    """Create a short-lived signed confirmation token for gated provider actions."""

    expires_at = int(time.time()) + ttl_seconds
    payload = f"{tool_id}:{connection_id}:{expires_at}"
    signature = hmac.new(
        _confirmation_secret(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    token = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(token.encode()).decode()


def _valid_confirmation_token(
    token: str | None,
    *,
    tool_id: str,
    connection_id: str | None,
) -> bool:
    if not token or not connection_id:
        return False
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        signed_tool_id, signed_connection_id, expires_at_text, signature = (
            decoded.rsplit(":", 3)
        )
        expires_at = int(expires_at_text)
    except Exception:
        return False
    if (
        signed_tool_id != tool_id
        or signed_connection_id != connection_id
        or expires_at < int(time.time())
    ):
        return False
    payload = f"{signed_tool_id}:{signed_connection_id}:{expires_at}"
    expected = hmac.new(
        _confirmation_secret(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def sync_integrations(
    session: Session,
    body: IntegrationCatalogSyncRequest,
) -> IntegrationCatalogSyncResponse:
    """Sync native and provider-backed integration catalogs through one path."""

    handler = LIVE_CATALOG_SYNC_HANDLERS.get(body.backend_id)
    if handler and not body.apps and not body.tools:
        return handler(session, body)
    summary = _sync_catalog_rows(session, body)
    return IntegrationCatalogSyncResponse(
        status="success",
        apps_upserted=summary["apps_upserted"],
        tools_upserted=summary["tools_upserted"],
        requested_app_slugs=body.app_slugs,
        matched_app_slugs=[
            str(app.get("canonical_app_slug") or app.get("provider_app_id") or "")
            for app in body.apps
            if app.get("canonical_app_slug") or app.get("provider_app_id")
        ],
        sync_mode=body.sync_mode,
        cache_version=body.cache_version,
    )


def _sync_catalog_rows(
    session: Session,
    body: IntegrationCatalogSyncRequest,
) -> dict[str, int]:
    """Import normalized provider apps/actions into the dynamic catalog."""

    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    apps_upserted = 0
    tools_upserted = 0
    action_previews_by_app: dict[tuple[str, str], list[dict[str, Any]]] = {}
    app_keys_to_index: set[tuple[str, str]] = set()

    for app_data in body.apps:
        provider_app_id = app_data["provider_app_id"]
        canonical_app_slug = _slugify(
            app_data.get("canonical_app_slug") or provider_app_id,
        )
        raw_metadata = dict(app_data.get("raw_provider_metadata") or {})
        source_type = app_data.get("source_type") or body.source_type
        raw_metadata.setdefault("source_type", source_type)
        if source_type == "native":
            raw_metadata.setdefault(
                "native_metadata",
                {
                    "tier": app_data.get("tier"),
                    "quality": app_data.get("quality"),
                    "capabilities": app_data.get("capabilities") or [],
                    "function_names": app_data.get("function_names") or [],
                    "guidance_titles": app_data.get("guidance_titles") or [],
                    "required_secrets": app_data.get("required_secrets") or [],
                    "optional_secrets": app_data.get("optional_secrets") or [],
                    "homepage": app_data.get("homepage"),
                    "tags": app_data.get("tags") or [],
                },
            )
        values = {
            "backend_id": body.backend_id,
            "provider_app_id": provider_app_id,
            "canonical_app_slug": canonical_app_slug,
            "display_name": app_data.get("display_name")
            or canonical_app_slug.replace("_", " ").title(),
            "description": app_data.get("description"),
            "category": app_data.get("category"),
            "icon_url": app_data.get("icon_url"),
            "auth_modes": app_data.get("auth_modes") or ["oauth"],
            "available_scopes_json": app_data.get("available_scopes") or [],
            "available_actions_json": app_data.get("available_actions") or [],
            "raw_provider_metadata_json": {
                **raw_metadata,
                **(
                    {"api_key_schema": app_data["api_key_schema"]}
                    if app_data.get("api_key_schema")
                    else {}
                ),
            },
            "cache_version": body.cache_version,
        }
        dao.upsert_catalog_app(
            backend_id=body.backend_id,
            provider_app_id=provider_app_id,
            canonical_app_slug=canonical_app_slug,
            values=values,
        )
        app_keys_to_index.add((body.backend_id, provider_app_id))
        apps_upserted += 1

    for tool_data in body.tools:
        provider_app_id = tool_data["provider_app_id"]
        canonical_app_slug = _slugify(
            tool_data.get("canonical_app_slug") or provider_app_id,
        )
        name = _slugify(tool_data.get("name") or tool_data["provider_tool_id"])
        tool_id = f"{body.backend_id}:{canonical_app_slug}:{name}"
        provider_tool_id = tool_data["provider_tool_id"]
        display_name = tool_data.get("display_name") or name.replace("_", " ").title()
        description = tool_data.get("description") or display_name
        required_scopes = tool_data.get("required_scopes") or []
        search_text = " ".join(
            [
                canonical_app_slug,
                provider_app_id,
                name,
                display_name,
                description,
                " ".join(required_scopes),
            ],
        ).lower()
        values = {
            "tool_id": tool_id,
            "backend_id": body.backend_id,
            "provider_app_id": provider_app_id,
            "canonical_app_slug": canonical_app_slug,
            "provider_tool_id": provider_tool_id,
            "unify_tool_id": f"primitives.integrations.{canonical_app_slug}.{name}",
            "canonical_name": f"primitives.integrations.{canonical_app_slug}.{name}",
            "function_manager_name": f"primitives_integrations__{canonical_app_slug}__{name}",
            "name": name,
            "display_name": display_name,
            "description": description,
            "tags_json": tool_data.get("tags") or [],
            "category": tool_data.get("category"),
            "input_schema_json": tool_data.get("input_schema") or {"type": "object"},
            "output_schema_json": tool_data.get("output_schema") or {"type": "object"},
            "required_scopes_json": required_scopes,
            "action_class": tool_data.get("action_class") or "read",
            "data_categories_json": tool_data.get("data_categories") or [],
            "examples_json": tool_data.get("examples") or [],
            "provider_raw_metadata_json": tool_data.get("raw_provider_metadata") or {},
            "search_text": tool_data.get("search_text") or search_text,
            "embedding_ref": tool_data.get("embedding_ref"),
            "confirmation_required": bool(
                tool_data.get("confirmation_required", False),
            ),
        }
        dao.upsert_catalog_tool(tool_id=tool_id, values=values)
        action_previews_by_app.setdefault(
            (body.backend_id, provider_app_id),
            [],
        ).append(
            {
                "id": tool_id,
                "name": name,
                "display_name": display_name,
                "description": description,
                "activation_state": "not_connected",
                "action_class": values["action_class"],
            },
        )
        tools_upserted += 1

    for (
        backend_id,
        provider_app_id,
    ), action_previews in action_previews_by_app.items():
        app = dao.get_app_by_backend_provider(
            backend_id=backend_id,
            provider_app_id=provider_app_id,
        )
        if app:
            dao.set_app_action_previews(app, action_previews)
            app_keys_to_index.add((backend_id, provider_app_id))

    if app_keys_to_index:
        session.flush()
        apps_to_index = dao.catalog_apps_for_keys(app_keys_to_index)
        _index_app_catalog_embeddings(session, apps_to_index)
    session.commit()
    return {"apps_upserted": apps_upserted, "tools_upserted": tools_upserted}


def _composio_live_catalog_handler(
    session: Session,
    body: IntegrationCatalogSyncRequest,
) -> IntegrationCatalogSyncResponse:
    """Fetch and normalize a bounded Composio catalog into provider tables."""

    seed_default_provider_catalog(session)
    backend = IntegrationProviderDAO(session).get_backend("composio")
    config = (backend.config_json if backend else {}) or {}
    adapter = get_provider_adapter(
        "composio",
        backend_config=config,
        backend_status=backend.status if backend else "enabled",
        require_live=True,
    )
    requested_slugs = [slug.strip().upper() for slug in body.app_slugs if slug.strip()]
    requested_set = set(requested_slugs)
    toolkits = adapter.list_toolkits()
    toolkits_by_slug = {
        str(
            toolkit.get("slug")
            or toolkit.get("toolkit_slug")
            or toolkit.get("id")
            or "",
        ).upper(): toolkit
        for toolkit in toolkits
        if toolkit.get("slug") or toolkit.get("toolkit_slug") or toolkit.get("id")
    }
    # No app/tool allowlist is applied by Orchestra. If callers provide
    # ``app_slugs`` we sync that explicit subset; otherwise we sync every app
    # returned by the enabled backend adapter. Backend status/config is the only
    # deployment-level gate.
    selected_toolkit_slugs = (
        sorted(toolkits_by_slug)
        if body.include_all_managed_apps or not requested_slugs
        else [slug for slug in requested_slugs if slug in toolkits_by_slug]
    )

    skipped_apps = [
        {"slug": slug, "reason": "not_found"}
        for slug in requested_slugs
        if slug not in toolkits_by_slug
    ]
    if requested_slugs and not selected_toolkit_slugs:
        error_message = "No requested Composio apps matched the live provider catalog."
        return IntegrationCatalogSyncResponse(
            status="failed",
            apps_upserted=0,
            tools_upserted=0,
            skipped_apps=skipped_apps,
            requested_app_slugs=requested_slugs,
            matched_app_slugs=[],
            sync_mode=body.sync_mode or "partial",
            error=error_message,
            warning=error_message,
            cache_version=body.cache_version,
        )
    apps: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    auth_configs_created = 0
    auth_configs_reused = 0
    should_create_auth_configs = body.create_auth_configs and bool(requested_slugs)

    for toolkit_slug in selected_toolkit_slugs:
        toolkit = toolkits_by_slug[toolkit_slug]
        canonical_app_slug = _composio_canonical_app_slug(toolkit_slug)
        auth_config_id = None
        if should_create_auth_configs and "oauth" in _composio_auth_modes(toolkit):
            try:
                auth_config_id = adapter.get_or_create_auth_config(toolkit_slug)
            except Exception as exc:
                skipped_apps.append(
                    {
                        "slug": toolkit_slug,
                        "reason": "auth_config_failed",
                        "message": str(exc)[:300],
                    },
                )
                continue
            if getattr(adapter, "last_auth_config_was_created", False):
                auth_configs_created += 1
            elif auth_config_id:
                auth_configs_reused += 1
        raw_provider_metadata = {
            "source": "composio_live_sync",
            "toolkit_slug": toolkit_slug,
            "toolkit_version": toolkit.get("version"),
            "managed_auth": True,
            "raw_toolkit": toolkit,
        }
        if auth_config_id:
            raw_provider_metadata["auth_config_id"] = auth_config_id
        apps.append(
            {
                "provider_app_id": toolkit_slug,
                "canonical_app_slug": canonical_app_slug,
                "display_name": toolkit.get("name")
                or canonical_app_slug.replace("_", " ").title(),
                "description": toolkit.get("description"),
                "category": toolkit.get("category"),
                "icon_url": _composio_icon_url(toolkit),
                "auth_modes": _composio_auth_modes(toolkit),
                "available_scopes": [],
                "raw_provider_metadata": raw_provider_metadata,
            },
        )
        tool_limit = body.tool_limit_per_app if body.tool_limit_per_app > 0 else None
        for tool in adapter.list_tools(toolkit_slug=toolkit_slug, limit=tool_limit):
            provider_tool_id = str(tool.get("slug") or tool.get("id") or "")
            if not provider_tool_id:
                continue
            tool_app_slug = (_composio_toolkit_slug(tool) or toolkit_slug).upper()
            if (
                not body.include_all_managed_apps
                and requested_set
                and tool_app_slug not in requested_set
                and tool_app_slug != toolkit_slug
            ):
                continue
            tool_name = _composio_tool_name(provider_tool_id, toolkit_slug)
            action_class = _composio_action_class(tool, canonical_app_slug)
            tools.append(
                {
                    "provider_app_id": toolkit_slug,
                    "canonical_app_slug": canonical_app_slug,
                    "provider_tool_id": provider_tool_id,
                    "name": tool_name,
                    "display_name": tool.get("name")
                    or tool_name.replace("_", " ").title(),
                    "description": tool.get("description")
                    or tool_name.replace("_", " ").title(),
                    "required_scopes": _composio_tool_scopes(tool),
                    "input_schema": _composio_tool_input_schema(tool),
                    "output_schema": _composio_tool_output_schema(tool),
                    "action_class": action_class,
                    "confirmation_required": action_class
                    in {"write", "destructive", "bulk_export"},
                    "category": toolkit.get("category"),
                    "tags": [
                        canonical_app_slug,
                        str(toolkit.get("category") or "").lower(),
                    ],
                    "raw_provider_metadata": {
                        "source": "composio_live_sync",
                        "toolkit_slug": toolkit_slug,
                        "tool_version": tool.get("version"),
                        "raw_tool": tool,
                    },
                },
            )

    if selected_toolkit_slugs and not apps:
        error_message = "No Composio apps produced catalog rows during live sync."
        return IntegrationCatalogSyncResponse(
            status="failed",
            apps_upserted=0,
            tools_upserted=0,
            skipped_apps=skipped_apps,
            requested_app_slugs=requested_slugs,
            matched_app_slugs=[],
            sync_mode=body.sync_mode
            or ("full" if body.include_all_managed_apps else "partial"),
            error=error_message,
            warning=error_message,
            auth_configs_created=auth_configs_created,
            auth_configs_reused=auth_configs_reused,
            cache_version=body.cache_version,
        )

    sync_body = IntegrationCatalogSyncRequest(
        backend_id="composio",
        cache_version=(
            body.cache_version
            if body.cache_version != "provider-sync-v1"
            else "composio-live-v1"
        ),
        apps=apps,
        tools=tools,
    )
    summary = _sync_catalog_rows(
        session,
        sync_body,
    )
    return IntegrationCatalogSyncResponse(
        status="success",
        apps_upserted=summary["apps_upserted"],
        tools_upserted=summary["tools_upserted"],
        skipped_apps=skipped_apps,
        requested_app_slugs=requested_slugs,
        matched_app_slugs=[
            str(app["canonical_app_slug"])
            for app in apps
            if app.get("canonical_app_slug")
        ],
        sync_mode=body.sync_mode
        or ("full" if body.include_all_managed_apps else "partial"),
        auth_configs_created=auth_configs_created,
        auth_configs_reused=auth_configs_reused,
        cache_version=sync_body.cache_version,
    )


def _pipedream_live_catalog_handler(
    session: Session,
    body: IntegrationCatalogSyncRequest,
) -> IntegrationCatalogSyncResponse:
    """Fetch and normalize Pipedream apps/actions through bounded provider pagination."""

    seed_default_provider_catalog(session)
    backend = IntegrationProviderDAO(session).get_backend("pipedream")
    config = (backend.config_json if backend else {}) or {}
    adapter = get_provider_adapter(
        "pipedream",
        backend_config=config,
        backend_status=backend.status if backend else "enabled",
        require_live=True,
    )
    requested_slugs = {_slugify(slug) for slug in body.app_slugs if slug.strip()}
    provider_apps = adapter.list_apps(has_components=True)
    selected_apps: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for provider_app in provider_apps:
        canonical_app_slug = _pipedream_app_slug(provider_app)
        seen_slugs.add(canonical_app_slug)
        if requested_slugs and canonical_app_slug not in requested_slugs:
            continue
        if not body.include_all_apps and not requested_slugs:
            continue
        selected_apps.append(provider_app)

    skipped_apps = [
        {"slug": slug, "reason": "not_found"}
        for slug in sorted(requested_slugs)
        if slug not in seen_slugs
    ]
    if requested_slugs and not selected_apps:
        error_message = "No requested Pipedream apps matched the live provider catalog."
        return IntegrationCatalogSyncResponse(
            status="failed",
            apps_upserted=0,
            tools_upserted=0,
            skipped_apps=skipped_apps,
            requested_app_slugs=sorted(requested_slugs),
            matched_app_slugs=[],
            sync_mode=body.sync_mode or "partial",
            error=error_message,
            warning=error_message,
            cache_version=body.cache_version,
        )
    apps: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    component_limit = (
        body.component_limit_per_app if body.component_limit_per_app > 0 else None
    )

    for provider_app in selected_apps:
        canonical_app_slug = _pipedream_app_slug(provider_app)
        provider_app_id = str(
            provider_app.get("id")
            or provider_app.get("name_slug")
            or provider_app.get("slug")
            or canonical_app_slug,
        )
        apps.append(
            {
                "provider_app_id": provider_app_id,
                "canonical_app_slug": canonical_app_slug,
                "display_name": provider_app.get("name")
                or canonical_app_slug.replace("_", " ").title(),
                "description": provider_app.get("description"),
                "category": _pipedream_category(provider_app),
                "icon_url": provider_app.get("img_src")
                or provider_app.get("logo_url")
                or provider_app.get("logoUrl"),
                "auth_modes": ["oauth"],
                "available_scopes": [],
                "raw_provider_metadata": {
                    "source": "pipedream_live_sync",
                    "raw_app": provider_app,
                },
            },
        )
        for component in adapter.list_components(
            app=provider_app_id,
            limit=component_limit,
            component_type="action",
        ):
            provider_tool_id = str(
                component.get("key")
                or component.get("id")
                or component.get("name_slug")
                or component.get("name")
                or "",
            )
            if not provider_tool_id:
                continue
            tool_name = _pipedream_tool_name(provider_tool_id, canonical_app_slug)
            description = (
                component.get("description")
                or component.get("name")
                or tool_name.replace("_", " ").title()
            )
            action_class = _provider_action_class(
                provider_tool_id=provider_tool_id,
                name=str(component.get("name") or ""),
                description=str(description or ""),
                canonical_app_slug=canonical_app_slug,
            )
            tools.append(
                {
                    "provider_app_id": provider_app_id,
                    "canonical_app_slug": canonical_app_slug,
                    "provider_tool_id": provider_tool_id,
                    "name": tool_name,
                    "display_name": component.get("name")
                    or tool_name.replace("_", " ").title(),
                    "description": description,
                    "required_scopes": [],
                    "input_schema": _pipedream_input_schema(component),
                    "output_schema": {"type": "object"},
                    "action_class": action_class,
                    "confirmation_required": action_class
                    in {"write", "destructive", "bulk_export"},
                    "category": _pipedream_category(provider_app),
                    "tags": [canonical_app_slug, "pipedream"],
                    "raw_provider_metadata": {
                        "source": "pipedream_live_sync",
                        "raw_component": component,
                    },
                },
            )

    sync_body = IntegrationCatalogSyncRequest(
        backend_id="pipedream",
        cache_version=(
            body.cache_version
            if body.cache_version != "provider-sync-v1"
            else "pipedream-live-v1"
        ),
        apps=apps,
        tools=tools,
    )
    summary = _sync_catalog_rows(
        session,
        sync_body,
    )
    return IntegrationCatalogSyncResponse(
        status="success",
        apps_upserted=summary["apps_upserted"],
        tools_upserted=summary["tools_upserted"],
        skipped_apps=skipped_apps,
        requested_app_slugs=sorted(requested_slugs),
        matched_app_slugs=[_pipedream_app_slug(app) for app in selected_apps],
        sync_mode=body.sync_mode or ("full" if body.include_all_apps else "partial"),
        cache_version=sync_body.cache_version,
    )


LIVE_CATALOG_SYNC_HANDLERS: dict[
    str,
    Callable[[Session, IntegrationCatalogSyncRequest], IntegrationCatalogSyncResponse],
] = {
    "composio": _composio_live_catalog_handler,
    "pipedream": _pipedream_live_catalog_handler,
}


def _capability_ids(capabilities: list[Any]) -> list[str]:
    """Normalize action previews or raw names into connection capability IDs."""

    ids: list[str] = []
    for capability in capabilities:
        if isinstance(capability, str):
            ids.append(capability)
            continue
        if isinstance(capability, dict):
            capability_id = (
                capability.get("id")
                or capability.get("name")
                or capability.get("canonical_name")
            )
            if capability_id:
                ids.append(str(capability_id))
    return ids


def _append_query_params(url: str | None, params: dict[str, str | None]) -> str | None:
    """Append callback state without losing existing provider/user query params."""

    if not url:
        return None
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value:
            query[key] = value
    return urlunparse(parsed._replace(query=urlencode(query)))


def _connection_to_response(
    conn: IntegrationConnection,
) -> IntegrationConnectionResponse:
    return IntegrationConnectionResponse(
        connection_id=conn.connection_id,
        owner_scope=conn.owner_scope,
        org_id=conn.org_id,
        team_id=conn.team_id,
        user_id=conn.user_id,
        assistant_id=conn.assistant_id,
        canonical_app_slug=conn.canonical_app_slug,
        backend_id=conn.backend_id,
        provider_app_id=conn.provider_app_id,
        provider_connection_id=conn.provider_connection_id,
        status=conn.status,
        external_account_label=conn.external_account_label,
        granted_scopes=conn.granted_scopes_json or [],
        enabled_capabilities=_capability_ids(conn.enabled_capabilities_json or []),
        disabled_actions=_disabled_action_ids(conn),
        tool_policy=_connection_tool_policy(conn),
        credential_storage=conn.credential_storage,
        secret_refs=conn.secret_refs_json or {},
        last_health_check_at=conn.last_health_check_at,
        last_health_check_status=conn.last_health_check_status,
        reconnect_reason=conn.reconnect_reason,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
    )


def _tool_keys(tool: ProviderToolCatalog) -> set[str]:
    return {
        key
        for key in {tool.tool_id, tool.provider_tool_id, tool.canonical_name, tool.name}
        if key
    }


def _default_tool_policy_level(tool: ProviderToolCatalog) -> str:
    if not tool.enabled_by_default:
        return "forbidden"
    if tool.confirmation_required or tool.action_class in {
        "write",
        "destructive",
        "bulk_export",
        "sensitive_read",
    }:
        return "specific_approval"
    return "auto"


def _connection_tool_policy(conn: IntegrationConnection | None) -> dict[str, str]:
    if not conn:
        return {}
    raw = conn.disabled_actions_json or []
    if isinstance(raw, dict):
        policy = raw.get("tool_policy") or raw.get("tools") or {}
        if isinstance(policy, dict):
            return {
                str(tool_id): str(level)
                for tool_id, level in policy.items()
                if level in {"auto", "specific_approval", "forbidden"}
            }
        return {}
    if isinstance(raw, list):
        return {str(tool_id): "forbidden" for tool_id in raw if tool_id}
    return {}


def _disabled_action_ids(conn: IntegrationConnection | None) -> list[str]:
    return sorted(
        tool_id
        for tool_id, level in _connection_tool_policy(conn).items()
        if level == "forbidden"
    )


def _effective_tool_policy_level(
    tool: ProviderToolCatalog,
    conn: IntegrationConnection | None,
) -> str:
    policy = _connection_tool_policy(conn)
    for key in _tool_keys(tool):
        if key in policy:
            return policy[key]
    return _default_tool_policy_level(tool)


def _owner_filter(query, owner: OwnerContext):
    return IntegrationProviderDAO(query.session).owner_filter(query, owner)


def _owner_external_user_id(
    owner: OwnerContext,
    fallback: str | None = None,
) -> str | None:
    if owner.user_id:
        return owner.user_id
    if owner.assistant_id is not None:
        return f"assistant:{owner.assistant_id}"
    if owner.team_id is not None:
        return f"team:{owner.team_id}"
    if owner.org_id is not None:
        return f"org:{owner.org_id}"
    return fallback


def _owner_tokens(owner: OwnerContext) -> set[str]:
    tokens = {owner.owner_scope}
    if owner.org_id is not None:
        tokens.update({str(owner.org_id), f"org:{owner.org_id}"})
    if owner.team_id is not None:
        tokens.update({str(owner.team_id), f"team:{owner.team_id}"})
    if owner.user_id:
        tokens.update({owner.user_id, f"user:{owner.user_id}"})
    if owner.assistant_id is not None:
        tokens.update({str(owner.assistant_id), f"assistant:{owner.assistant_id}"})
    return tokens


def _provider_exception_details(exc: Exception) -> tuple[int | None, str]:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    response_text = getattr(response, "text", "") if response is not None else ""
    return status_code, str(response_text or "")[:1000]


def _log_composio_connect_failure(
    *,
    stage: str,
    connection: IntegrationConnection,
    owner: OwnerContext,
    app: DynamicProviderApp | None,
    exc: Exception,
) -> None:
    status_code, response_text = _provider_exception_details(exc)
    logger.exception(
        "Composio connect failure stage=%s backend_id=%s provider_app_id=%s "
        "canonical_app_slug=%s connection_id=%s owner_scope=%s "
        "provider_status_code=%s provider_response=%s",
        stage,
        connection.backend_id,
        connection.provider_app_id,
        app.canonical_app_slug if app else connection.canonical_app_slug,
        connection.connection_id,
        owner.owner_scope,
        status_code,
        response_text,
    )


def _provider_connect_url(
    *,
    backend: IntegrationBackend | None,
    app: DynamicProviderApp | None = None,
    owner: OwnerContext,
    connection: IntegrationConnection,
    redirect_url: Optional[str],
) -> str:
    config = (backend.config_json if backend else {}) or {}
    backend_status = backend.status if backend else "enabled"
    external_user_id = (
        _owner_external_user_id(owner, connection.connection_id)
        or connection.connection_id
    )
    callback_url = _append_query_params(
        redirect_url,
        {"connection_id": connection.connection_id},
    )
    if connection.backend_id == "composio":
        adapter = get_provider_adapter(
            connection.backend_id,
            backend_config=config,
            backend_status=backend_status,
        )
        if hasattr(adapter, "create_auth_link"):
            auth_config_id = (
                (app.raw_provider_metadata_json if app else {}) or {}
            ).get("auth_config_id")
            if not auth_config_id:
                if not hasattr(adapter, "get_or_create_auth_config"):
                    raise ValueError(
                        f"Composio auth_config_id is required to connect {connection.provider_app_id}.",
                    )
                try:
                    auth_config_id = adapter.get_or_create_auth_config(
                        connection.provider_app_id,
                    )
                except Exception as exc:
                    _log_composio_connect_failure(
                        stage="auth_config_create",
                        connection=connection,
                        owner=owner,
                        app=app,
                        exc=exc,
                    )
                    raise
                if not auth_config_id:
                    logger.warning(
                        "Composio auth config creation returned no id backend_id=%s "
                        "provider_app_id=%s canonical_app_slug=%s connection_id=%s "
                        "owner_scope=%s",
                        connection.backend_id,
                        connection.provider_app_id,
                        (
                            app.canonical_app_slug
                            if app
                            else connection.canonical_app_slug
                        ),
                        connection.connection_id,
                        owner.owner_scope,
                    )
                    raise ValueError(
                        f"Composio auth_config_id is required to connect {connection.provider_app_id}.",
                    )
                if app:
                    app.raw_provider_metadata_json = {
                        **(app.raw_provider_metadata_json or {}),
                        "auth_config_id": str(auth_config_id),
                    }
            try:
                connect_url, connected_account_id, error = adapter.create_auth_link(
                    user_id=external_user_id,
                    auth_config_id=str(auth_config_id),
                    callback_url=callback_url,
                    alias=connection.connection_id,
                )
            except Exception as exc:
                _log_composio_connect_failure(
                    stage="auth_link_create",
                    connection=connection,
                    owner=owner,
                    app=app,
                    exc=exc,
                )
                raise
            if error:
                logger.warning(
                    "Composio auth link returned provider error backend_id=%s "
                    "provider_app_id=%s canonical_app_slug=%s connection_id=%s "
                    "owner_scope=%s error=%s",
                    connection.backend_id,
                    connection.provider_app_id,
                    app.canonical_app_slug if app else connection.canonical_app_slug,
                    connection.connection_id,
                    owner.owner_scope,
                    error,
                )
                raise ValueError(error["message"])
            if connected_account_id:
                connection.provider_connection_id = connected_account_id
            if connect_url:
                return connect_url
        base = "https://backend.composio.dev/api/v3.1/connected_accounts/link"
        params = {
            "connection_id": connection.connection_id,
            "user_id": external_user_id,
            "toolkit_slug": connection.provider_app_id,
        }
        if callback_url:
            params["redirect_url"] = callback_url
        return f"{base}?{urlencode(params)}"
    if connection.backend_id == "pipedream":
        adapter = get_provider_adapter(
            connection.backend_id,
            backend_config=config,
            backend_status=backend_status,
        )
        if hasattr(adapter, "create_connect_link_url"):
            connect_link_url, error = adapter.create_connect_link_url(
                external_user_id=external_user_id,
                redirect_url=callback_url,
                allowed_origins=config.get("allowed_origins") or None,
            )
            if error:
                raise ValueError(error["message"])
            if connect_link_url:
                return connect_link_url
        base = "/integrations/provider-oauth/pipedream"
        params = {
            "connection_id": connection.connection_id,
            "external_user_id": external_user_id,
        }
        if callback_url:
            params["redirect_url"] = callback_url
        return f"{base}?{urlencode(params)}"
    return (
        f"/integrations/provider-oauth/mock?connection_id={connection.connection_id}"
        f"&redirect_url={callback_url or ''}"
    )


def list_connections(
    session: Session,
    owner: OwnerContext,
    *,
    include_disconnected: bool = False,
) -> list[IntegrationConnectionResponse]:
    seed_default_provider_catalog(session)
    connections = IntegrationProviderDAO(session).list_connections(
        owner,
        include_disconnected=include_disconnected,
    )
    session.flush()
    return [_connection_to_response(conn) for conn in connections]


def _best_connection(
    session: Session,
    *,
    owner: OwnerContext,
    canonical_app_slug: str,
    connection_id: Optional[str] = None,
) -> Optional[IntegrationConnection]:
    conn = IntegrationProviderDAO(session).best_connection(
        owner=owner,
        canonical_app_slug=canonical_app_slug,
        connection_id=connection_id,
    )
    session.flush()
    return conn


def _tool_to_search_result(
    *,
    tool: ProviderToolCatalog,
    app: DynamicProviderApp | None,
    conn: IntegrationConnection | None,
    match_reason: str = "app detail",
    score: float = 0.0,
) -> ProviderToolSearchResult:
    return ProviderToolSearchResult(
        tool_id=tool.tool_id,
        backend_id=tool.backend_id,
        provider_app_id=tool.provider_app_id,
        provider_tool_id=tool.provider_tool_id,
        canonical_name=tool.canonical_name,
        function_manager_name=tool.function_manager_name,
        app_slug=tool.canonical_app_slug,
        app_display_name=app.display_name if app else tool.canonical_app_slug,
        app_icon_url=app.icon_url if app else None,
        tool_display_name=tool.display_name,
        description=tool.description,
        match_reason=match_reason,
        activation_state=_activation_state(tool, conn),
        action_class=tool.action_class,
        required_scopes=tool.required_scopes_json or [],
        connection_id=conn.connection_id if conn else None,
        confirmation_required=_tool_requires_confirmation(tool, conn, None),
        approval_level=_effective_tool_policy_level(tool, conn),
        score=score,
    )


def _scope_label(scope_id: str) -> str:
    cleaned = (
        scope_id.replace(":", " ").replace(".", " ").replace("_", " ").replace("-", " ")
    )
    return " ".join(part.capitalize() for part in cleaned.split()) or scope_id


def _derive_scopes(
    app: DynamicProviderApp,
    tools: list[ProviderToolCatalog],
) -> list[dict[str, Any]]:
    scopes_by_id: dict[str, dict[str, Any]] = {}
    for index, scope in enumerate(app.available_scopes_json or []):
        if isinstance(scope, str):
            scope_id = scope
            scopes_by_id[scope_id] = {
                "id": scope_id,
                "label": _scope_label(scope_id),
                "required": True,
            }
        elif isinstance(scope, dict):
            scope_id = str(
                scope.get("id")
                or scope.get("name")
                or scope.get("scope")
                or f"scope-{index}",
            )
            scopes_by_id[scope_id] = {
                "id": scope_id,
                "label": scope.get("label")
                or scope.get("display_name")
                or _scope_label(scope_id),
                "description": scope.get("description"),
                "required": scope.get("required", True),
            }
    for tool in tools:
        for scope_id in tool.required_scopes_json or []:
            if not scope_id:
                continue
            scopes_by_id.setdefault(
                str(scope_id),
                {
                    "id": str(scope_id),
                    "label": _scope_label(str(scope_id)),
                    "required": True,
                },
            )
    return list(scopes_by_id.values())


def _tool_preview(
    tool: ProviderToolCatalog,
    conn: IntegrationConnection | None,
) -> dict[str, Any]:
    return {
        "id": tool.tool_id,
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "activation_state": _activation_state(tool, conn),
        "action_class": tool.action_class,
        "provider_tool_id": tool.provider_tool_id,
        "canonical_name": tool.canonical_name,
        "required_scopes": tool.required_scopes_json or [],
        "confirmation_required": _tool_requires_confirmation(tool, conn, None),
        "approval_level": _effective_tool_policy_level(tool, conn),
    }


def _app_metadata(app: DynamicProviderApp) -> dict[str, Any]:
    return (
        app.raw_provider_metadata_json
        if isinstance(app.raw_provider_metadata_json, dict)
        else {}
    )


def _app_source_type(app: DynamicProviderApp) -> str:
    """Return the explicit source lane for unified app discovery.

    Native apps are Unity-deploy packages projected into the global catalog for
    search only; provider-backed apps are third-party catalog rows with
    connection and policy state owned by Orchestra.
    """

    metadata = _app_metadata(app)
    if metadata.get("source_type") == "native" or app.backend_id == "unity_native":
        return "native"
    return "third_party"


def _app_source_label(app: DynamicProviderApp) -> str:
    return "Native" if _app_source_type(app) == "native" else "Third-party"


def _app_embedding_ref_id(app: DynamicProviderApp) -> str:
    return f"{app.backend_id}:{app.canonical_app_slug}"


def _index_app_catalog_embeddings(
    session: Session,
    apps: list[DynamicProviderApp],
) -> int:
    """Batch-index app catalog rows for source-agnostic semantic discovery."""

    if not apps:
        return 0
    _rows, changed_count = CATALOG_ARTIFACT_EMBEDDINGS.upsert_many(
        session,
        namespace=INTEGRATION_APP_EMBEDDING_NAMESPACE,
        artifacts=[
            {
                "ref_id": _app_embedding_ref_id(app),
                "source_text": _app_catalog_embedding_text(app),
                "metadata": {
                    "backend_id": app.backend_id,
                    "provider_app_id": app.provider_app_id,
                    "canonical_app_slug": app.canonical_app_slug,
                    "display_name": app.display_name,
                    "source_type": _app_source_type(app),
                },
            }
            for app in apps
        ],
    )
    return changed_count


def _active_backend_ids(session: Session) -> set[str]:
    seed_default_provider_catalog(session)
    return IntegrationProviderDAO(session).active_backend_ids()


def _app_response(
    session: Session,
    *,
    app: DynamicProviderApp,
    owner: OwnerContext,
    overlay: IntegrationOverlay | None,
) -> DynamicIntegrationAppResponse:
    conn = _best_connection(
        session,
        owner=owner,
        canonical_app_slug=app.canonical_app_slug,
    )
    metadata = _app_metadata(app)
    source_type = _app_source_type(app)
    tool_count = IntegrationProviderDAO(session).tool_count_for_app(
        app.canonical_app_slug,
    )
    return DynamicIntegrationAppResponse(
        backend_id=app.backend_id,
        provider_app_id=app.provider_app_id,
        canonical_app_slug=app.canonical_app_slug,
        display_name=app.display_name,
        source_type=source_type,
        source_label=_app_source_label(app),
        description=app.description,
        category=app.category,
        icon_url=app.icon_url,
        auth_modes=app.auth_modes or [],
        available_scopes=app.available_scopes_json or [],
        available_actions=[],
        tool_count=tool_count,
        api_key_schema=metadata.get("api_key_schema"),
        connection_status=conn.status if conn else None,
        connection_id=conn.connection_id if conn else None,
        external_account_label=conn.external_account_label if conn else None,
        overlay=overlay.display_overrides_json if overlay else {},
        native_metadata=(
            metadata.get("native_metadata") if source_type == "native" else {}
        ),
    )


def _score_app_match(app: DynamicProviderApp, query_text: str) -> tuple[float, str]:
    normalized = query_text.strip().lower()
    if not normalized:
        return 1.0, "all supported integrations"
    metadata = app.raw_provider_metadata_json or {}
    tags = metadata.get("tags") if isinstance(metadata, dict) else None
    fields = [
        app.canonical_app_slug,
        app.display_name,
        app.description or "",
        app.category or "",
        app.provider_app_id,
        app.backend_id,
        " ".join(str(tag) for tag in tags) if isinstance(tags, list) else "",
    ]
    haystack = " ".join(str(field).lower() for field in fields)
    slug = app.canonical_app_slug.lower()
    display = app.display_name.lower()
    if normalized == slug or normalized == display:
        return 100.0, "exact app match"
    if normalized in slug or normalized in display:
        return 80.0, "app name match"
    if normalized in haystack:
        return 60.0, "catalog metadata match"
    tokens = [
        token
        for token in normalized.replace("-", " ").replace("_", " ").split()
        if token
    ]
    if not tokens:
        return 0.0, ""
    matched = [token for token in tokens if token in haystack]
    if not matched:
        return 0.0, ""
    return 10.0 * len(matched) / len(tokens), f"matched terms: {', '.join(matched)}"


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key} {_json_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(_json_text(item) for item in value)
    return str(value)


def _app_catalog_embedding_text(app: DynamicProviderApp) -> str:
    metadata = app.raw_provider_metadata_json or {}
    native_metadata = (
        metadata.get("native_metadata") if isinstance(metadata, dict) else {}
    )
    if not isinstance(native_metadata, dict):
        native_metadata = {}
    return " ".join(
        str(part)
        for part in [
            app.canonical_app_slug,
            app.display_name,
            app.provider_app_id,
            app.backend_id,
            app.description or "",
            app.category or "",
            _json_text(app.auth_modes),
            _json_text(app.available_scopes_json),
            _json_text(app.available_actions_json),
            _json_text(metadata.get("tags") if isinstance(metadata, dict) else ""),
            _json_text(metadata.get("synonyms") if isinstance(metadata, dict) else ""),
            _json_text(metadata.get("examples") if isinstance(metadata, dict) else ""),
            _json_text(native_metadata.get("tier")),
            _json_text(native_metadata.get("quality")),
            _json_text(native_metadata.get("capabilities")),
            _json_text(native_metadata.get("function_names")),
            _json_text(native_metadata.get("guidance_titles")),
            _json_text(native_metadata.get("required_secrets")),
            _json_text(native_metadata.get("optional_secrets")),
            _json_text(native_metadata.get("homepage")),
        ]
        if part
    )


def _tool_catalog_embedding_text(
    tool: ProviderToolCatalog,
    app: DynamicProviderApp | None = None,
) -> str:
    metadata = tool.provider_raw_metadata_json or {}
    return " ".join(
        str(part)
        for part in [
            tool.canonical_name,
            tool.function_manager_name,
            tool.tool_id,
            tool.provider_tool_id,
            tool.unify_tool_id,
            tool.name,
            tool.display_name,
            tool.description,
            tool.search_text,
            tool.canonical_app_slug,
            app.display_name if app else "",
            app.description if app else "",
            tool.backend_id,
            tool.provider_app_id,
            tool.category or "",
            tool.action_class,
            _json_text(tool.tags_json),
            _json_text(tool.required_scopes_json),
            _json_text(tool.data_categories_json),
            _json_text(tool.input_schema_json),
            _json_text(tool.output_schema_json),
            _json_text(tool.examples_json),
            _json_text(
                metadata.get("example_prompts") if isinstance(metadata, dict) else "",
            ),
            _json_text(metadata.get("synonyms") if isinstance(metadata, dict) else ""),
        ]
        if part
    )


def _semantic_app_scores(
    session: Session,
    query_text: str,
    apps: list[DynamicProviderApp],
) -> dict[str, tuple[float, str]]:
    if not query_text:
        return {}
    ref_ids = [_app_embedding_ref_id(app) for app in apps]
    raw_scores = CATALOG_ARTIFACT_EMBEDDINGS.search(
        session,
        namespace=INTEGRATION_APP_EMBEDDING_NAMESPACE,
        query_text=query_text,
        ref_ids=ref_ids,
        limit=len(apps) or 1,
    )
    if not raw_scores and apps:
        # Catalog search should normally be read-only because sync pre-indexes
        # apps. This fallback covers local/dev databases created before the
        # batch indexer ran, without putting one embedding call per app on the
        # steady-state search path.
        _index_app_catalog_embeddings(session, apps)
        session.flush()
        raw_scores = CATALOG_ARTIFACT_EMBEDDINGS.search(
            session,
            namespace=INTEGRATION_APP_EMBEDDING_NAMESPACE,
            query_text=query_text,
            ref_ids=ref_ids,
            limit=len(apps) or 1,
        )
    return {
        ref_id: (score * 100.0, reason)
        for ref_id, (score, reason) in raw_scores.items()
        if score >= GLOBAL_CATALOG_SEMANTIC_SCORE_CUTOFF
    }


def list_apps(
    session: Session,
    *,
    query_text: str = "",
    owner: OwnerContext,
    source_type: str | None = None,
) -> list[DynamicIntegrationAppResponse]:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    overlays = dao.list_overlays_by_slug()
    results: list[DynamicIntegrationAppResponse] = []
    for app in dao.list_enabled_apps(query_text=query_text):
        if source_type and _app_source_type(app) != source_type:
            continue
        overlay = overlays.get(app.canonical_app_slug)
        results.append(_app_response(session, app=app, owner=owner, overlay=overlay))
    return results


def get_apps(session: Session, body: ProviderAppGetRequest) -> ProviderAppGetResponse:
    seed_default_provider_catalog(session)
    owner = OwnerContext(
        owner_scope=body.owner_scope,
        org_id=body.org_id,
        team_id=body.team_id,
        user_id=body.user_id,
        assistant_id=body.assistant_id,
    )
    items = list_apps(
        session,
        query_text=body.query,
        owner=owner,
        source_type=body.source_type,
    )
    total = len(items)
    return ProviderAppGetResponse(
        items=items[body.offset : body.offset + body.limit],
        total=total,
        limit=body.limit,
        offset=body.offset,
    )


def search_apps(
    session: Session,
    body: ProviderAppSearchRequest,
) -> list[ProviderAppSearchResult]:
    seed_default_provider_catalog(session)
    owner = OwnerContext(
        owner_scope=body.owner_scope,
        org_id=body.org_id,
        team_id=body.team_id,
        user_id=body.user_id,
        assistant_id=body.assistant_id,
    )
    dao = IntegrationProviderDAO(session)
    overlays = dao.list_overlays_by_slug()
    apps = dao.list_enabled_apps()
    if body.source_type:
        apps = [app for app in apps if _app_source_type(app) == body.source_type]
    semantic_scores = _semantic_app_scores(session, body.query.strip(), apps)
    scored: list[ProviderAppSearchResult] = []
    for app in apps:
        score, reason = _score_app_match(app, body.query)
        semantic_score, semantic_reason = semantic_scores.get(
            _app_embedding_ref_id(app),
            (0.0, ""),
        )
        if body.query.strip() and score <= 0 and semantic_score <= 0:
            continue
        if semantic_score > 0:
            score += semantic_score
            if semantic_score >= score - semantic_score:
                reason = semantic_reason
        base = _app_response(
            session,
            app=app,
            owner=owner,
            overlay=overlays.get(app.canonical_app_slug),
        )
        scored.append(
            ProviderAppSearchResult(
                **base.model_dump(),
                supported=True,
                score=score,
                match_reason=reason,
            ),
        )
    scored.sort(key=lambda item: (-item.score, item.display_name.lower()))
    return scored[body.offset : body.offset + body.limit]


def get_app_detail(
    session: Session,
    *,
    canonical_app_slug: str,
    owner: OwnerContext,
) -> IntegrationAppDetailResponse:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    app = dao.get_app_by_slug(canonical_app_slug)
    if not app:
        raise ValueError(f"Unknown integration app: {canonical_app_slug}")
    overlay = dao.get_overlay(canonical_app_slug)
    conn = _best_connection(session, owner=owner, canonical_app_slug=canonical_app_slug)
    tools = dao.list_tools(canonical_app_slug=canonical_app_slug)
    derived_scopes = _derive_scopes(app, tools)
    tool_results = [
        _tool_to_search_result(
            tool=tool,
            app=app,
            conn=conn,
            match_reason="app detail",
            score=1.0,
        )
        for tool in tools
    ]
    metadata = _app_metadata(app)
    source_type = _app_source_type(app)
    return IntegrationAppDetailResponse(
        backend_id=app.backend_id,
        provider_app_id=app.provider_app_id,
        canonical_app_slug=app.canonical_app_slug,
        display_name=app.display_name,
        source_type=source_type,
        source_label=_app_source_label(app),
        description=app.description,
        category=app.category,
        icon_url=app.icon_url,
        auth_modes=app.auth_modes or [],
        available_scopes=derived_scopes,
        available_actions=[_tool_preview(tool, conn) for tool in tools],
        tool_count=len(tools),
        api_key_schema=metadata.get("api_key_schema"),
        connection_status=conn.status if conn else None,
        connection_id=conn.connection_id if conn else None,
        external_account_label=conn.external_account_label if conn else None,
        overlay=overlay.display_overrides_json if overlay else {},
        native_metadata=(
            metadata.get("native_metadata") if source_type == "native" else {}
        ),
        tools=[tool.model_dump() for tool in tool_results],
        derived_scopes=derived_scopes,
    )


def start_connection(
    session: Session,
    *,
    owner: OwnerContext,
    canonical_app_slug: str,
    backend_id: Optional[str],
    requested_scopes: list[str],
    auth_mode: Optional[str],
    api_key_fields: dict[str, str],
    created_by: Optional[str],
    redirect_url: Optional[str],
    account_label: Optional[str] = None,
) -> tuple[IntegrationConnectionResponse, Optional[str], str, bool]:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    app = dao.get_app_by_slug(canonical_app_slug, backend_id=backend_id)
    if not app:
        raise ValueError(f"Unknown integration app: {canonical_app_slug}")
    backend = dao.get_backend(app.backend_id)
    if not backend or backend.status != "enabled":
        raise ValueError(f"Integration backend is disabled: {app.backend_id}")
    if _app_source_type(app) == "native":
        raise ValueError(
            "Native Unity-deploy integrations are deployment-enabled and do not create provider connections.",
        )

    chosen_auth_mode = auth_mode or ((app.auth_modes or ["oauth"])[0])
    status = (
        "connected" if chosen_auth_mode == "api_key" and api_key_fields else "pending"
    )
    credential_storage = (
        "secret_manager" if chosen_auth_mode == "api_key" else "provider_vault"
    )
    connection = dao.create_connection(
        {
            "owner_scope": owner.owner_scope,
            "org_id": owner.org_id,
            "team_id": owner.team_id,
            "user_id": owner.user_id,
            "assistant_id": owner.assistant_id,
            "canonical_app_slug": app.canonical_app_slug,
            "backend_id": app.backend_id,
            "provider_app_id": app.provider_app_id,
            "provider_connection_id": (
                f"local_{uuid.uuid4().hex}" if status == "connected" else None
            ),
            "status": status,
            "granted_scopes_json": requested_scopes,
            "enabled_capabilities_json": _capability_ids(
                app.available_actions_json or [],
            ),
            "credential_storage": credential_storage,
            "secret_refs_json": {key: "<redacted>" for key in api_key_fields},
            "external_account_label": _normalize_account_label(account_label),
            "created_by": created_by,
            "reconnect_reason": (
                None if status == "connected" else "authorization_required"
            ),
        },
    )
    session.commit()

    connect_url = None
    if chosen_auth_mode == "oauth":
        connect_url = _provider_connect_url(
            backend=backend,
            app=app,
            owner=owner,
            connection=connection,
            redirect_url=redirect_url,
        )
        session.commit()
    return (
        _connection_to_response(connection),
        connect_url,
        chosen_auth_mode,
        chosen_auth_mode == "oauth",
    )


def complete_connection(
    session: Session,
    *,
    connection_id: str,
    provider_connection_id: Optional[str],
    granted_scopes: list[str],
    external_account_label: Optional[str],
    status: str,
    reconnect_reason: Optional[str] = None,
) -> IntegrationConnectionResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    updates = {
        "provider_connection_id": provider_connection_id
        or conn.provider_connection_id
        or f"local_{uuid.uuid4().hex}",
        "granted_scopes_json": granted_scopes or conn.granted_scopes_json or [],
        "status": status,
        "reconnect_reason": (
            None
            if status == "connected"
            else (reconnect_reason or conn.reconnect_reason)
        ),
    }
    if external_account_label is not None:
        updates["external_account_label"] = _normalize_account_label(
            external_account_label,
        )
    dao.update_connection_fields(conn, **updates)
    session.commit()
    return _connection_to_response(conn)


def complete_connection_by_provider_connection_id(
    session: Session,
    *,
    provider_connection_id: str,
    owner: OwnerContext,
    granted_scopes: list[str],
    external_account_label: Optional[str],
    status: str,
    reconnect_reason: Optional[str] = None,
) -> IntegrationConnectionResponse:
    if not any(
        [
            owner.org_id is not None,
            owner.team_id is not None,
            bool(owner.user_id),
            owner.assistant_id is not None,
        ],
    ):
        raise ValueError("Owner scope is required to complete provider connection")
    conn = IntegrationProviderDAO(session).find_connection_by_provider_id(
        provider_connection_id=provider_connection_id,
        owner=owner,
    )
    if not conn:
        raise ValueError(f"Unknown provider connection: {provider_connection_id}")
    return complete_connection(
        session,
        connection_id=conn.connection_id,
        provider_connection_id=provider_connection_id,
        granted_scopes=granted_scopes,
        external_account_label=external_account_label,
        status=status,
        reconnect_reason=reconnect_reason,
    )


def disconnect_connection(
    session: Session,
    connection_id: str,
) -> IntegrationConnectionResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    dao.update_connection_fields(
        conn,
        status="disconnected",
        reconnect_reason="user_disconnected",
    )
    session.commit()
    return _connection_to_response(conn)


def cancel_connection(
    session: Session,
    connection_id: str,
) -> IntegrationConnectionResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    if conn.status not in {"pending", "error", "expired", "revoked", "missing_secrets"}:
        raise ValueError(
            f"Connection {connection_id} cannot be cancelled from status {conn.status}.",
        )
    dao.update_connection_fields(
        conn,
        status="disconnected",
        reconnect_reason="setup_cancelled",
    )
    session.commit()
    return _connection_to_response(conn)


def reconnect_connection(
    session: Session,
    connection_id: str,
) -> IntegrationConnectionResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    dao.update_connection_fields(
        conn,
        status="pending",
        reconnect_reason="authorization_required",
    )
    session.commit()
    return _connection_to_response(conn)


def update_connection(
    session: Session,
    connection_id: str,
    *,
    account_label: Optional[str],
) -> IntegrationConnectionResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    dao.update_connection_fields(
        conn,
        external_account_label=_normalize_account_label(account_label),
    )
    session.commit()
    return _connection_to_response(conn)


def test_connection(
    session: Session,
    connection_id: str,
) -> tuple[IntegrationConnectionResponse, str]:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    backend = dao.get_backend(conn.backend_id)
    adapter = get_provider_adapter(
        conn.backend_id,
        backend_config=(backend.config_json if backend else {}),
        backend_status=backend.status if backend else "enabled",
    )
    if conn.status == "connected":
        result = adapter.health_check(
            ProviderExecutionRequest(
                backend_id=conn.backend_id,
                tool_id="health_check",
                canonical_app_slug=conn.canonical_app_slug,
                provider_tool_id="health_check",
                connection_id=conn.connection_id,
                provider_connection_id=conn.provider_connection_id,
                action_class="read",
                user_id=conn.user_id,
            ),
        )
        updates: dict[str, Any] = {
            "last_health_check_status": "ok" if result.status == "ok" else "error",
        }
        if result.status != "ok":
            updates["status"] = "error"
            updates["reconnect_reason"] = (result.error or {}).get(
                "code",
                "provider_health_check_failed",
            )
        dao.update_connection_fields(conn, **updates)
    else:
        dao.update_connection_fields(conn, last_health_check_status=conn.status)
    session.commit()
    return _connection_to_response(conn), conn.last_health_check_status or "unknown"


def get_connection_tool_policy(
    session: Session,
    connection_id: str,
) -> IntegrationToolPolicyResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    tools = dao.list_tools(canonical_app_slug=conn.canonical_app_slug)
    return IntegrationToolPolicyResponse(
        connection_id=conn.connection_id,
        canonical_app_slug=conn.canonical_app_slug,
        policies=[
            IntegrationToolPolicyItem(
                tool_id=tool.tool_id,
                provider_tool_id=tool.provider_tool_id,
                canonical_name=tool.canonical_name,
                display_name=tool.display_name,
                action_class=tool.action_class,
                default_approval_level=_default_tool_policy_level(tool),
                approval_level=_effective_tool_policy_level(tool, conn),
                activation_state=_activation_state(tool, conn),
                confirmation_required=_tool_requires_confirmation(tool, conn, None),
            )
            for tool in tools
        ],
    )


def patch_connection_tool_policy(
    session: Session,
    connection_id: str,
    body: IntegrationToolPolicyPatchRequest,
) -> IntegrationToolPolicyResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    tools = dao.list_tools(canonical_app_slug=conn.canonical_app_slug)
    policy = {} if body.reset_to_defaults else dict(_connection_tool_policy(conn))
    tools_by_key: dict[str, ProviderToolCatalog] = {}
    for tool in tools:
        for key in _tool_keys(tool):
            tools_by_key[key] = tool

    if body.bulk_approval_level:
        selected_action_classes = set(body.action_classes or [])
        for tool in tools:
            if (
                selected_action_classes
                and tool.action_class not in selected_action_classes
            ):
                continue
            policy[tool.tool_id] = body.bulk_approval_level
    for tool_id, approval_level in body.tool_policies.items():
        tool = tools_by_key.get(tool_id)
        policy[tool.tool_id if tool else tool_id] = approval_level

    dao.set_connection_tool_policy(conn, policy)
    session.commit()
    return get_connection_tool_policy(session, connection_id)


def _activation_state(
    tool: ProviderToolCatalog,
    conn: Optional[IntegrationConnection],
) -> str:
    if not tool.enabled_by_default:
        return "disabled_by_policy"
    if _effective_tool_policy_level(tool, conn) == "forbidden":
        return "disabled_by_policy"
    if not conn or conn.status in {"disconnected", "pending", "missing_secrets"}:
        return "not_connected"
    if conn.status in EXPIRED_STATUSES:
        return "expired" if conn.status != "error" else "error"
    required = set(tool.required_scopes_json or [])
    granted = set(conn.granted_scopes_json or [])
    if required and not required.issubset(granted):
        return "missing_scope"
    return "connected_ready"


def _policy_error(
    *,
    backend: IntegrationBackend | None,
    overlay: IntegrationOverlay | None,
    tool: ProviderToolCatalog,
    conn: IntegrationConnection | None,
    owner: OwnerContext,
) -> dict[str, Any] | None:
    if backend and backend.status != "enabled":
        return {
            "code": "backend_disabled",
            "message": f"{backend.display_name} is disabled for provider-backed execution.",
        }
    allowed = set((backend.allowed_orgs_or_tenants if backend else []) or [])
    if allowed and allowed.isdisjoint(_owner_tokens(owner)):
        return {
            "code": "tenant_not_allowed",
            "message": "This provider backend is not enabled for the current owner.",
        }
    if _effective_tool_policy_level(tool, conn) == "forbidden":
        return {
            "code": "action_disabled_for_connection",
            "message": "This action is disabled for the selected connection.",
        }
    policy = (overlay.action_policy_json if overlay else {}) or {}
    denied = set(policy.get("deny_actions") or policy.get("disabled_actions") or [])
    if denied.intersection(_tool_keys(tool)):
        return {
            "code": "action_disabled_by_overlay",
            "message": "This action is disabled by integration policy.",
        }
    return None


def _tool_requires_confirmation(
    tool: ProviderToolCatalog,
    conn: IntegrationConnection | None,
    overlay: IntegrationOverlay | None,
) -> bool:
    approval_level = _effective_tool_policy_level(tool, conn)
    if approval_level == "auto":
        return False
    if approval_level == "specific_approval":
        return True
    if tool.confirmation_required or tool.action_class in {
        "write",
        "destructive",
        "bulk_export",
    }:
        return True
    policy = (overlay.action_policy_json if overlay else {}) or {}
    confirm_actions = set(policy.get("confirmation_required_actions") or [])
    return bool(confirm_actions.intersection(_tool_keys(tool)))


def _score_tool(
    tool: ProviderToolCatalog,
    query_text: str,
    conn: Optional[IntegrationConnection],
) -> tuple[float, str]:
    if not query_text:
        return float(tool.overlay_rank_boost or 0), "default catalog order"
    tokens = {tok for tok in query_text.lower().replace("_", " ").split() if tok}
    haystack = (tool.search_text or "").lower()
    exact = 5.0 if query_text.lower() in haystack else 0.0
    token_hits = sum(1 for token in tokens if token in haystack)
    connected = 2.0 if conn and conn.status == "connected" else 0.0
    score = exact + token_hits + connected + float(tool.overlay_rank_boost or 0)
    if exact:
        reason = "exact metadata match"
    elif token_hits:
        reason = f"{token_hits} keyword matches in normalized provider metadata"
    else:
        reason = "semantic-ready catalog candidate"
    return score, reason


def _lexical_signal(tool: ProviderToolCatalog, query_text: str) -> float:
    if not query_text:
        return 1.0
    normalized_query = query_text.lower()
    tokens = {tok for tok in normalized_query.replace("_", " ").split() if tok}
    haystack = " ".join(
        [
            tool.search_text or "",
            tool.canonical_name or "",
            tool.display_name or "",
            tool.description or "",
            tool.canonical_app_slug or "",
        ],
    ).lower()
    exact = 5.0 if normalized_query in haystack else 0.0
    return exact + float(sum(1 for token in tokens if token in haystack))


def _semantic_tool_scores(
    session: Session,
    query_text: str,
    tools: list[ProviderToolCatalog],
) -> dict[str, tuple[float, str]]:
    if not query_text:
        return {}
    if _SEMANTIC_TOOL_SCORE_PROVIDER is not None:
        raw_scores = _SEMANTIC_TOOL_SCORE_PROVIDER(session, query_text, tools) or {}
    else:
        raw_scores = _artifact_tool_scores(session, query_text, tools)
    normalized: dict[str, tuple[float, str]] = {}
    for tool_id, value in raw_scores.items():
        if isinstance(value, tuple):
            score, reason = value
        else:
            score = value
            reason = "embedding similarity over provider tool metadata"
        try:
            normalized[tool_id] = (float(score), reason)
        except (TypeError, ValueError):
            continue
    return normalized


def _artifact_tool_scores(
    session: Session,
    query_text: str,
    tools: list[ProviderToolCatalog],
) -> dict[str, tuple[float, str]]:
    if not tools:
        return {}
    dao = IntegrationProviderDAO(session)
    apps = {app.canonical_app_slug: app for app in dao.list_all_apps()}
    for tool in tools:
        CATALOG_ARTIFACT_EMBEDDINGS.upsert(
            session,
            namespace=INTEGRATION_TOOL_EMBEDDING_NAMESPACE,
            ref_id=tool.tool_id,
            source_text=_tool_catalog_embedding_text(
                tool,
                apps.get(tool.canonical_app_slug),
            ),
            metadata={
                "backend_id": tool.backend_id,
                "canonical_app_slug": tool.canonical_app_slug,
                "provider_tool_id": tool.provider_tool_id,
                "canonical_name": tool.canonical_name,
            },
        )
    session.flush()
    raw_scores = CATALOG_ARTIFACT_EMBEDDINGS.search(
        session,
        namespace=INTEGRATION_TOOL_EMBEDDING_NAMESPACE,
        query_text=query_text,
        ref_ids=[tool.tool_id for tool in tools],
        limit=len(tools),
    )
    return {
        tool_id: (score, reason)
        for tool_id, (score, reason) in raw_scores.items()
        if score >= GLOBAL_CATALOG_SEMANTIC_SCORE_CUTOFF
    }


def _tool_search_result(
    *,
    tool: ProviderToolCatalog,
    app: DynamicProviderApp | None,
    conn: IntegrationConnection | None,
    activation_state: str,
    match_reason: str,
    score: float,
) -> ProviderToolSearchResult:
    return ProviderToolSearchResult(
        tool_id=tool.tool_id,
        backend_id=tool.backend_id,
        provider_app_id=tool.provider_app_id,
        provider_tool_id=tool.provider_tool_id,
        canonical_name=tool.canonical_name,
        function_manager_name=tool.function_manager_name,
        app_slug=tool.canonical_app_slug,
        app_display_name=app.display_name if app else tool.canonical_app_slug,
        app_icon_url=app.icon_url if app else None,
        tool_display_name=tool.display_name,
        description=tool.description,
        match_reason=match_reason,
        activation_state=activation_state,
        action_class=tool.action_class,
        required_scopes=tool.required_scopes_json or [],
        connection_id=conn.connection_id if conn else None,
        confirmation_required=_tool_requires_confirmation(tool, conn, None),
        approval_level=_effective_tool_policy_level(tool, conn),
        score=score,
    )


def get_tools(
    session: Session,
    body: ProviderToolGetRequest,
) -> ProviderToolGetResponse:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    owner = OwnerContext(
        owner_scope=body.owner_scope,
        org_id=body.org_id,
        team_id=body.team_id,
        user_id=body.user_id,
        assistant_id=body.assistant_id,
    )
    tools = dao.list_tools(canonical_app_slug=body.canonical_app_slug)
    apps = {app.canonical_app_slug: app for app in dao.list_all_apps()}
    results: list[ProviderToolSearchResult] = []
    for tool in tools:
        conn = _best_connection(
            session,
            owner=owner,
            canonical_app_slug=tool.canonical_app_slug,
        )
        activation_state = _activation_state(tool, conn)
        if body.activation_state and activation_state != body.activation_state:
            continue
        if not body.include_unconnected and activation_state != "connected_ready":
            continue
        results.append(
            _tool_search_result(
                tool=tool,
                app=apps.get(tool.canonical_app_slug),
                conn=conn,
                activation_state=activation_state,
                match_reason="filtered provider tool",
                score=float(tool.overlay_rank_boost or 0),
            ),
        )
    total = len(results)
    return ProviderToolGetResponse(
        items=results[body.offset : body.offset + body.limit],
        total=total,
        limit=body.limit,
        offset=body.offset,
    )


def search_tools(
    session: Session,
    body: ProviderToolSearchRequest,
) -> list[ProviderToolSearchResult]:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    owner = OwnerContext(
        owner_scope=body.owner_scope,
        org_id=body.org_id,
        team_id=body.team_id,
        user_id=body.user_id,
        assistant_id=body.assistant_id,
    )
    query_text = body.query.strip()
    tools = dao.list_tools(canonical_app_slug=body.canonical_app_slug)
    semantic_scores = _semantic_tool_scores(session, query_text, tools)

    apps = {app.canonical_app_slug: app for app in dao.list_all_apps()}
    results: list[ProviderToolSearchResult] = []
    for tool in tools:
        conn = _best_connection(
            session,
            owner=owner,
            canonical_app_slug=tool.canonical_app_slug,
        )
        activation_state = _activation_state(tool, conn)
        if not body.include_unconnected and activation_state != "connected_ready":
            continue
        score, reason = _score_tool(tool, query_text, conn)
        semantic_score, semantic_reason = semantic_scores.get(tool.tool_id, (0.0, ""))
        if (
            query_text
            and _lexical_signal(tool, query_text) <= 0
            and semantic_score <= 0
        ):
            continue
        if semantic_score > 0:
            score += semantic_score
            if semantic_score >= score - semantic_score:
                reason = semantic_reason
        results.append(
            _tool_search_result(
                tool=tool,
                app=apps.get(tool.canonical_app_slug),
                conn=conn,
                activation_state=activation_state,
                match_reason=reason,
                score=score,
            ),
        )
    sorted_results = sorted(results, key=lambda item: item.score, reverse=True)
    return sorted_results[body.offset : body.offset + body.limit]


def get_tool_schema(
    session: Session,
    *,
    tool_id: str,
    owner: OwnerContext,
) -> ProviderToolSchemaResponse:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    tool = dao.get_tool(tool_id)
    if not tool:
        raise ValueError(f"Unknown provider tool: {tool_id}")
    conn = _best_connection(
        session,
        owner=owner,
        canonical_app_slug=tool.canonical_app_slug,
    )
    app = dao.get_app_by_slug(tool.canonical_app_slug)
    return ProviderToolSchemaResponse(
        tool_id=tool.tool_id,
        backend_id=tool.backend_id,
        provider_app_id=tool.provider_app_id,
        provider_tool_id=tool.provider_tool_id,
        canonical_name=tool.canonical_name,
        function_manager_name=tool.function_manager_name,
        app_slug=tool.canonical_app_slug,
        app_display_name=app.display_name if app else tool.canonical_app_slug,
        app_icon_url=app.icon_url if app else None,
        tool_display_name=tool.display_name,
        description=tool.description,
        input_schema=tool.input_schema_json or {},
        output_schema=tool.output_schema_json or {},
        required_scopes=tool.required_scopes_json or [],
        action_class=tool.action_class,
        confirmation_required=_tool_requires_confirmation(tool, conn, None),
        approval_level=_effective_tool_policy_level(tool, conn),
        examples=tool.examples_json or [],
        activation_state=_activation_state(tool, conn),
        connection_id=conn.connection_id if conn else None,
    )


def _redact_summary(payload: dict[str, Any]) -> str:
    keys = sorted(payload.keys())
    return f"keys={keys[:20]}"


def run_tool(
    session: Session,
    *,
    tool_id: str,
    body: ProviderToolRunRequest,
) -> ProviderToolRunResponse:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    start = time.monotonic()
    owner = OwnerContext(
        owner_scope=body.owner_scope,
        org_id=body.org_id,
        team_id=body.team_id,
        user_id=body.user_id,
        assistant_id=body.assistant_id,
    )
    tool = dao.get_tool(tool_id)
    if not tool:
        raise ValueError(f"Unknown provider tool: {tool_id}")
    backend = dao.get_backend(tool.backend_id)
    overlay = dao.get_overlay(tool.canonical_app_slug)
    conn = _best_connection(
        session,
        owner=owner,
        canonical_app_slug=tool.canonical_app_slug,
        connection_id=body.connection_id,
    )
    activation_state = _activation_state(tool, conn)
    policy_error = _policy_error(
        backend=backend,
        overlay=overlay,
        tool=tool,
        conn=conn,
        owner=owner,
    )

    error: Optional[dict[str, Any]] = None
    status = "ok"
    result: dict[str, Any] = {}
    if policy_error:
        status = "blocked_by_policy"
        error = policy_error
    elif activation_state == "not_connected":
        status = "connect_required"
        error = {
            "code": "connect_required",
            "message": f"Connect {tool.canonical_app_slug} in Console before using this tool.",
        }
    elif activation_state == "missing_scope":
        status = "missing_scope"
        error = {
            "code": "missing_scope",
            "message": "Reconnect this integration with the required scopes.",
            "required_scopes": tool.required_scopes_json or [],
        }
    elif activation_state == "disabled_by_policy":
        status = "blocked_by_policy"
        error = {
            "code": "blocked_by_policy",
            "message": "This provider action is disabled by policy.",
        }
    elif activation_state in {"expired", "error"}:
        status = "reconnect_required"
        error = {
            "code": activation_state,
            "message": "Reconnect or test this integration in Console.",
        }
    elif (
        _tool_requires_confirmation(tool, conn, overlay) and not body.confirmation_token
    ):
        status = "confirmation_required"
        error = {
            "code": "confirmation_required",
            "message": "This action requires explicit confirmation before execution.",
        }
    elif _tool_requires_confirmation(
        tool,
        conn,
        overlay,
    ) and not _valid_confirmation_token(
        body.confirmation_token,
        tool_id=tool.tool_id,
        connection_id=conn.connection_id if conn else None,
    ):
        status = "confirmation_required"
        error = {
            "code": "invalid_confirmation",
            "message": "Confirmation token is invalid or expired.",
        }
    else:
        adapter = get_provider_adapter(
            tool.backend_id,
            backend_config=(backend.config_json if backend else {}),
            backend_status=backend.status if backend else "enabled",
        )
        adapter_result = adapter.execute(
            ProviderExecutionRequest(
                backend_id=tool.backend_id,
                tool_id=tool.tool_id,
                canonical_app_slug=tool.canonical_app_slug,
                provider_tool_id=tool.provider_tool_id,
                connection_id=conn.connection_id if conn else None,
                provider_connection_id=conn.provider_connection_id if conn else None,
                action_class=tool.action_class,
                user_id=body.user_id,
                arguments=body.arguments,
            ),
        )
        if adapter_result.status == "ok":
            result = adapter_result.result
        else:
            status = "provider_error"
            error = adapter_result.error or {
                "code": "provider_error",
                "message": "Provider execution failed.",
            }

    audit = dao.add_action_audit(
        {
            "org_id": body.org_id,
            "team_id": body.team_id,
            "user_id": body.user_id,
            "assistant_id": body.assistant_id,
            "conversation_id": body.conversation_id,
            "connection_id": conn.connection_id if conn else None,
            "backend_id": tool.backend_id,
            "canonical_app_slug": tool.canonical_app_slug,
            "provider_action_id": tool.provider_tool_id,
            "provider_tool_id": tool.provider_tool_id,
            "unify_tool_id": tool.unify_tool_id,
            "action_class": tool.action_class,
            "status": status,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "redacted_input_summary": _redact_summary(body.arguments),
            "redacted_output_summary": _redact_summary(result) if result else None,
            "error_code": error.get("code") if error else None,
        },
    )
    session.commit()

    return ProviderToolRunResponse(
        status=status,
        activation_state=activation_state,
        tool_id=tool.tool_id,
        connection_id=conn.connection_id if conn else None,
        result=result,
        error=error,
        audit_id=audit.id,
    )
