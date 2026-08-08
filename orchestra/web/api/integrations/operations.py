"""API-local integration operations.

The public integration contract is the HTTP API in ``views.py``. This module is
kept beside those routes to hold provider orchestration that is too large to
inline in route functions: catalog sync normalization, provider connect URL
construction, policy checks, and execution auditing.
Database persistence goes through ``IntegrationProviderDAO`` and external calls
go through provider adapters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.models.integration_provider_models import (
    IntegrationBackend,
    IntegrationConnection,
    IntegrationOverlay,
    ProviderActionAudit,
)
from orchestra.integrations.providers import (
    ProviderExecutionRequest,
    get_provider_adapter,
)

# Composio wire-format normalizers live in the Composio adapter (single source of
# truth). They are re-exported here under their historical names so the legacy
# catalog-sync handler and existing tests keep importing them from this module.
from orchestra.integrations.providers.composio import (  # noqa: F401
    _composio_action_class,
    _composio_auth_modes,
    _composio_behavior_hints,
    _composio_canonical_app_slug,
    _composio_icon_url,
    _composio_requires_custom_oauth,
    _composio_tool_input_schema,
    _composio_tool_name,
    _composio_tool_output_schema,
    _composio_tool_scopes,
    _composio_toolkit_slug,
)

# Pipedream wire-format normalizers live in the Pipedream adapter; re-exported
# here under their historical names for the legacy catalog-sync handler and tests.
from orchestra.integrations.providers.pipedream import (  # noqa: F401
    _pipedream_action_class,
    _pipedream_app_slug,
    _pipedream_behavior_hints,
    _pipedream_category,
    _pipedream_input_schema,
    _pipedream_tool_name,
)

# Provider-neutral conventions live in providers/utils; aliased to the historical
# underscore names still referenced by the legacy catalog-sync handlers below.
from orchestra.integrations.providers.utils.normalization import (
    action_class_from_behavior_hints as _action_class_from_behavior_hints,
)
from orchestra.integrations.providers.utils.normalization import slugify as _slugify
from orchestra.integrations.transient import (
    DEFAULT_MAX_ATTEMPTS,
    call_with_transient_retries,
    embedded_transient_ok_failures,
    is_safe_to_retry_ok_result,
    parse_retry_after_seconds,
    should_retry_adapter_result,
)
from orchestra.web.api.integrations.schema import (
    IntegrationAppPreferenceResponse,
    IntegrationCatalogSyncRequest,
    IntegrationCatalogSyncResponse,
    IntegrationConnectionResponse,
    IntegrationToolExecutionApprovalRequest,
    IntegrationToolExecutionApprovalResponse,
    IntegrationToolPolicyItem,
    IntegrationToolPolicyPatchRequest,
    IntegrationToolPolicyResponse,
    ProviderToolConfirmationPayload,
    ProviderToolRunRequest,
    ProviderToolRunResponse,
)

READY_STATUSES = {"connected"}
EXPIRED_STATUSES = {"expired", "revoked", "error"}
logger = logging.getLogger(__name__)


def _integration_onboarding_baseline(
    session: Session,
    *,
    assistant_id: int | None,
    connection_id: str | None = None,
) -> list[str] | None:
    """Snapshot onboarding progress before a connection mutation.

    Best-effort: lock timeouts / DB errors must not fail connect/disconnect.
    Rolls the session back on failure so the caller can still persist the
    connection change on a clean transaction.
    """
    from orchestra.services.coordinator_service import (
        onboarding_baseline_for_integration_connect,
    )

    try:
        return onboarding_baseline_for_integration_connect(
            session,
            assistant_id=assistant_id,
        )
    except Exception:
        logger.exception(
            "onboarding_baseline_failed connection_id=%s assistant_id=%s",
            connection_id,
            assistant_id,
        )
        session.rollback()
        return None


def _notify_integration_onboarding_connected(
    session: Session,
    *,
    assistant_id: int | None,
    canonical_app_slug: str,
    baseline_completed_step_ids: list[str] | None,
    connection_id: str | None = None,
) -> None:
    from orchestra.services.coordinator_service import (
        notify_coordinator_onboarding_after_integration_connected,
    )

    try:
        notify_coordinator_onboarding_after_integration_connected(
            session,
            assistant_id=assistant_id,
            canonical_app_slug=canonical_app_slug,
            baseline_completed_step_ids=baseline_completed_step_ids,
        )
    except Exception:
        logger.exception(
            "onboarding_notify_connected_failed connection_id=%s assistant_id=%s",
            connection_id,
            assistant_id,
        )


def _notify_integration_onboarding_disconnected(
    session: Session,
    *,
    assistant_id: int | None,
    canonical_app_slug: str,
    baseline_completed_step_ids: list[str] | None,
    connection_id: str | None = None,
) -> None:
    from orchestra.services.coordinator_service import (
        notify_coordinator_onboarding_after_integration_disconnected,
    )

    try:
        notify_coordinator_onboarding_after_integration_disconnected(
            session,
            assistant_id=assistant_id,
            canonical_app_slug=canonical_app_slug,
            baseline_completed_step_ids=baseline_completed_step_ids,
        )
    except Exception:
        logger.exception(
            "onboarding_notify_disconnected_failed connection_id=%s assistant_id=%s",
            connection_id,
            assistant_id,
        )


class ProviderConnectError(Exception):
    """A connect attempt failed for a reason worth surfacing to the caller.

    Carries an HTTP status + user-facing message so route handlers can return an
    actionable error instead of a bare 500 (e.g. a toolkit that needs a custom
    OAuth app before it can be connected).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        code: str = "provider_connect_failed",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _is_missing_managed_auth_error(exc: Exception) -> bool:
    """True when Auth-config creation rejected for lack of managed creds.

    These toolkits (e.g. TikTok) have no Composio-managed OAuth credentials, so
    the operator must supply their own OAuth app. Composio answers the managed
    ``POST /auth_configs`` with a 400 whose body mentions the missing default
    auth config.
    """

    status_code, response_text = _provider_exception_details(exc)
    if status_code not in (400, 404):
        return False
    haystack = f"{response_text} {exc}".lower()
    needles = (
        "default auth config not found",
        "defaultauthconfignotfound",
        "does not have managed",
        "no managed",
        "managed credentials",
        "no default auth config",
    )
    return any(needle in haystack for needle in needles)


@dataclass(frozen=True)
class OwnerContext:
    owner_scope: str = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None


@dataclass(frozen=True)
class RuntimeProviderTool:
    tool_id: str
    backend_id: str
    provider_app_id: str
    canonical_app_slug: str
    app_display_name: str
    app_icon_url: str | None
    provider_tool_id: str
    unify_tool_id: str
    canonical_name: str
    function_manager_name: str
    name: str
    display_name: str
    description: str
    action_class: str
    behavior_hints_json: list[str]
    required_scopes_json: list[str]
    input_schema_json: dict[str, Any]
    output_schema_json: dict[str, Any]
    examples_json: list[dict[str, Any]]
    enabled_by_default: bool = True
    confirmation_required: bool = False


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


def _normalize_account_label(value: Optional[str]) -> Optional[str]:
    return (value or "").strip() or None


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
        apps_pruned=summary.get("apps_pruned", 0),
        tools_pruned=summary.get("tools_pruned", 0),
        apps=list(body.apps),
        tools=list(body.tools),
        requested_app_slugs=body.app_slugs,
        matched_app_slugs=[
            str(app.get("canonical_app_slug") or app.get("provider_app_id") or "")
            for app in body.apps
            if app.get("canonical_app_slug") or app.get("provider_app_id")
        ],
        sync_mode=body.sync_mode,
        cache_version=body.cache_version,
        prune_unlisted_apps=_catalog_prune_requested(body),
    )


def _catalog_prune_requested(body: IntegrationCatalogSyncRequest) -> bool:
    return body.prune_unlisted_apps or body.sync_mode == "full"


def _sync_catalog_rows(
    session: Session,
    body: IntegrationCatalogSyncRequest,
) -> dict[str, int]:
    """Legacy DB projection writes are retired; Builtins logs own catalog rows."""

    return {
        "apps_upserted": 0,
        "tools_upserted": 0,
        "apps_pruned": 0,
        "tools_pruned": 0,
    }


def _composio_live_catalog_handler(
    session: Session,
    body: IntegrationCatalogSyncRequest,
) -> IntegrationCatalogSyncResponse:
    """Fetch and normalize a bounded Composio catalog into provider tables."""

    started_at = time.perf_counter()
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
    logger.info(
        "Composio catalog sync start mode=%s include_all=%s sync_tools=%s "
        "requested=%s selected=%s cache_version=%s",
        body.sync_mode or ("full" if body.include_all_managed_apps else "partial"),
        body.include_all_managed_apps,
        body.sync_tools,
        len(requested_slugs),
        len(selected_toolkit_slugs),
        body.cache_version,
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
    tool_fetch_started_at = time.perf_counter()
    syncable_toolkit_slugs: list[str] = []

    for index, toolkit_slug in enumerate(selected_toolkit_slugs, start=1):
        toolkit = toolkits_by_slug[toolkit_slug]
        canonical_app_slug = _composio_canonical_app_slug(
            toolkit_slug,
            toolkit.get("name"),
        )
        # A bring-your-own OAuth config (operator-supplied client credentials,
        # stored in the backend config) wins over Composio-managed auth and is
        # the only workable path for toolkits Composio has no managed
        # credentials for (e.g. TikTok).
        auth_config_id = _custom_auth_config_id(config, toolkit_slug)
        toolkit_auth_modes = _composio_auth_modes(toolkit)
        requires_custom_oauth = _composio_requires_custom_oauth(
            toolkit,
            None,
            toolkit_auth_modes,
        )
        managed_auth = not requires_custom_oauth
        custom_auth_configured = auth_config_id is not None
        if (
            not auth_config_id
            and should_create_auth_configs
            and "oauth" in toolkit_auth_modes
        ):
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
            "managed_auth": managed_auth,
            "requires_custom_oauth": requires_custom_oauth,
            "custom_auth_configured": custom_auth_configured,
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
                "auth_modes": toolkit_auth_modes,
                "available_scopes": [],
                "managed_auth": managed_auth,
                "requires_custom_oauth": requires_custom_oauth,
                "raw_provider_metadata": raw_provider_metadata,
            },
        )
        syncable_toolkit_slugs.append(toolkit_slug)
        if index == len(selected_toolkit_slugs) or index % 25 == 0:
            logger.info(
                "Composio catalog sync progress processed_toolkits=%s/%s "
                "apps=%s tools=%s elapsed_seconds=%.3f",
                index,
                len(selected_toolkit_slugs),
                len(apps),
                len(tools),
                time.perf_counter() - started_at,
            )

    if body.sync_tools and syncable_toolkit_slugs:
        tool_limit = body.tool_limit_per_app if body.tool_limit_per_app > 0 else None
        tool_fetch_concurrency = max(1, int(config.get("tool_fetch_concurrency", 8)))
        raw_tools_by_toolkit: dict[str, list[dict[str, Any]]] = {}

        def fetch_tools(toolkit_slug: str) -> tuple[str, list[dict[str, Any]]]:
            return (
                toolkit_slug,
                adapter.list_tools(toolkit_slug=toolkit_slug, limit=tool_limit),
            )

        logger.info(
            "Composio tool fetch start toolkits=%s concurrency=%s tool_limit_per_app=%s",
            len(syncable_toolkit_slugs),
            tool_fetch_concurrency,
            body.tool_limit_per_app,
        )
        with ThreadPoolExecutor(max_workers=tool_fetch_concurrency) as executor:
            future_by_slug = {
                executor.submit(fetch_tools, toolkit_slug): toolkit_slug
                for toolkit_slug in syncable_toolkit_slugs
            }
            for completed, future in enumerate(as_completed(future_by_slug), start=1):
                toolkit_slug, toolkit_tools = future.result()
                raw_tools_by_toolkit[toolkit_slug] = toolkit_tools
                if completed == len(future_by_slug) or completed % 25 == 0:
                    logger.info(
                        "Composio tool fetch progress completed_toolkits=%s/%s "
                        "latest_toolkit=%s latest_tools=%s elapsed_seconds=%.3f",
                        completed,
                        len(future_by_slug),
                        toolkit_slug,
                        len(toolkit_tools),
                        time.perf_counter() - tool_fetch_started_at,
                    )

        for toolkit_slug in syncable_toolkit_slugs:
            toolkit = toolkits_by_slug[toolkit_slug]
            canonical_app_slug = _composio_canonical_app_slug(
                toolkit_slug,
                toolkit.get("name"),
            )
            for tool in raw_tools_by_toolkit.get(toolkit_slug, []):
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
                behavior_hints = _composio_behavior_hints(tool)
                action_class = _action_class_from_behavior_hints(behavior_hints)
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
                        "behavior_hints": behavior_hints,
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

    db_started_at = time.perf_counter()
    sync_body = IntegrationCatalogSyncRequest(
        backend_id="composio",
        cache_version=(
            body.cache_version
            if body.cache_version != "provider-sync-v1"
            else "composio-live-v1"
        ),
        apps=apps,
        tools=tools,
        app_slugs=[
            _composio_canonical_app_slug(slug, toolkits_by_slug[slug].get("name"))
            for slug in selected_toolkit_slugs
        ],
        prune_unlisted_apps=_catalog_prune_requested(body),
    )
    summary = _sync_catalog_rows(
        session,
        sync_body,
    )
    elapsed = time.perf_counter() - started_at
    logger.info(
        "Composio catalog sync complete status=success apps=%s tools=%s "
        "selected_toolkits=%s tool_fetch_seconds=%.3f db_seconds=%.3f "
        "elapsed_seconds=%.3f cache_version=%s",
        summary["apps_upserted"],
        summary["tools_upserted"],
        len(selected_toolkit_slugs),
        db_started_at - tool_fetch_started_at,
        time.perf_counter() - db_started_at,
        elapsed,
        sync_body.cache_version,
    )
    return IntegrationCatalogSyncResponse(
        status="success",
        apps_upserted=summary["apps_upserted"],
        tools_upserted=summary["tools_upserted"],
        apps_pruned=summary.get("apps_pruned", 0),
        tools_pruned=summary.get("tools_pruned", 0),
        apps=apps,
        tools=tools,
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
        prune_unlisted_apps=_catalog_prune_requested(body),
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
            behavior_hints = _pipedream_behavior_hints(component)
            action_class = _action_class_from_behavior_hints(behavior_hints)
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
                    "behavior_hints": behavior_hints,
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
        app_slugs=[_pipedream_app_slug(app) for app in selected_apps],
        prune_unlisted_apps=_catalog_prune_requested(body),
    )
    summary = _sync_catalog_rows(
        session,
        sync_body,
    )
    return IntegrationCatalogSyncResponse(
        status="success",
        apps_upserted=summary["apps_upserted"],
        tools_upserted=summary["tools_upserted"],
        apps_pruned=summary.get("apps_pruned", 0),
        tools_pruned=summary.get("tools_pruned", 0),
        apps=apps,
        tools=tools,
        skipped_apps=skipped_apps,
        requested_app_slugs=sorted(requested_slugs),
        matched_app_slugs=[_pipedream_app_slug(app) for app in selected_apps],
        sync_mode=body.sync_mode or ("full" if body.include_all_apps else "partial"),
        cache_version=sync_body.cache_version,
        prune_unlisted_apps=_catalog_prune_requested(body),
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
        provider_user_id=conn.provider_user_id,
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


def _tool_keys(tool: RuntimeProviderTool) -> set[str]:
    return {
        key
        for key in {tool.tool_id, tool.provider_tool_id, tool.canonical_name, tool.name}
        if key
    }


def _default_tool_policy_level(tool: RuntimeProviderTool) -> str:
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
    tool: RuntimeProviderTool,
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


def _owner_is_empty(owner: OwnerContext | None) -> bool:
    return owner is None or (
        owner.org_id is None
        and owner.team_id is None
        and not owner.user_id
        and owner.assistant_id is None
    )


def _assert_connection_owner(
    dao: IntegrationProviderDAO,
    conn: IntegrationConnection,
    owner: OwnerContext | None,
) -> None:
    if _owner_is_empty(owner):
        return
    owned = dao.best_connection(
        owner=owner,
        canonical_app_slug=conn.canonical_app_slug,
        connection_id=conn.connection_id,
    )
    if not owned:
        raise PermissionError("Connection does not belong to the requested owner.")


def _assert_audit_owner(
    audit: ProviderActionAudit,
    owner: OwnerContext | None,
) -> None:
    if _owner_is_empty(owner):
        return
    assert owner is not None
    if owner.org_id is not None and audit.org_id != owner.org_id:
        raise PermissionError("Execution audit does not belong to the requested owner.")
    if owner.team_id is not None and audit.team_id != owner.team_id:
        raise PermissionError("Execution audit does not belong to the requested owner.")
    if owner.user_id and audit.user_id != owner.user_id:
        raise PermissionError("Execution audit does not belong to the requested owner.")
    if owner.assistant_id is not None and audit.assistant_id != owner.assistant_id:
        raise PermissionError("Execution audit does not belong to the requested owner.")


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
    app: Any | None,
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


CUSTOM_AUTH_CONFIG_KEY = "auth_config_overrides"


def _custom_auth_overrides(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the backend's bring-your-own-OAuth override map (slug -> config)."""

    overrides = (config or {}).get(CUSTOM_AUTH_CONFIG_KEY)
    return overrides if isinstance(overrides, dict) else {}


def _custom_auth_config_id(
    config: dict[str, Any] | None,
    toolkit_slug: str,
) -> str | None:
    """Resolve an operator-provided (custom OAuth) auth_config_id for a toolkit."""

    entry = _custom_auth_overrides(config).get(toolkit_slug)
    if isinstance(entry, dict):
        auth_config_id = entry.get("auth_config_id")
        return str(auth_config_id) if auth_config_id else None
    return None


def _require_backend(session: Session, backend_id: str) -> IntegrationBackend:
    seed_default_provider_catalog(session)
    backend = IntegrationProviderDAO(session).get_backend(backend_id)
    if not backend:
        raise ValueError(f"Unknown integration backend: {backend_id}")
    return backend


def list_custom_auth_configs(
    session: Session,
    *,
    backend_id: str,
) -> list[dict[str, Any]]:
    """Return operator-configured bring-your-own OAuth configs for a backend.

    Only non-secret metadata is stored/returned: the client id/secret live in
    the provider's vault, never in Orchestra.
    """

    backend = _require_backend(session, backend_id)
    overrides = _custom_auth_overrides(backend.config_json)
    return [
        {"backend_id": backend_id, "toolkit_slug": slug, **entry}
        for slug, entry in sorted(overrides.items())
        if isinstance(entry, dict)
    ]


def default_composio_oauth_redirect_uri() -> str:
    """White-label Composio OAuth callback on Orchestra's public API host."""

    raw = (
        (
            os.getenv("ORCHESTRA_PUBLIC_URL")
            or os.getenv("ORCHESTRA_URL")
            or "https://api.unify.ai"
        )
        .strip()
        .rstrip("/")
    )
    base_v0 = raw if raw.endswith("/v0") else f"{raw}/v0"
    return f"{base_v0}/integrations/composio/oauth/callback"


def set_custom_auth_config(
    session: Session,
    *,
    backend_id: str,
    toolkit_slug: str,
    client_id: str,
    client_secret: str,
    auth_scheme: str = "OAUTH2",
    scopes: Optional[list[str]] = None,
    display_name: Optional[str] = None,
    oauth_redirect_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Register a bring-your-own OAuth app for a toolkit on ``backend_id``.

    The client id/secret are handed to the provider (Composio), which vaults
    them and returns an ``auth_config_id``. Orchestra persists only that id plus
    non-secret metadata in the backend config, so subsequent connect flows use
    the operator's OAuth app instead of provider-managed credentials. Returns
    the stored (non-secret) config entry.
    """

    backend = _require_backend(session, backend_id)
    if backend.kind != "composio":
        raise ValueError(
            "Custom OAuth configs are currently only supported for Composio backends.",
        )
    slug = (toolkit_slug or "").strip().upper()
    if not slug:
        raise ValueError("toolkit_slug is required.")
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required.")

    adapter = get_provider_adapter(
        backend_id,
        backend_config=(backend.config_json or {}),
        backend_status=backend.status,
        require_live=True,
    )
    if not hasattr(adapter, "create_custom_auth_config"):
        raise ValueError(
            f"Backend {backend_id} does not support custom OAuth configs.",
        )
    scope_list = [s.strip() for s in (scopes or []) if s and s.strip()]
    resolved_redirect_uri = oauth_redirect_uri or default_composio_oauth_redirect_uri()
    auth_config_id = adapter.create_custom_auth_config(
        slug,
        client_id=client_id,
        client_secret=client_secret,
        auth_scheme=auth_scheme,
        scopes=scope_list or None,
        name=display_name,
        oauth_redirect_uri=resolved_redirect_uri,
    )

    now = datetime.now(timezone.utc).isoformat()
    config = dict(backend.config_json or {})
    overrides = dict(config.get(CUSTOM_AUTH_CONFIG_KEY) or {})
    key = slug.upper()
    previous = overrides.get(key)
    previous = previous if isinstance(previous, dict) else {}
    old_auth_config_id = previous.get("auth_config_id")
    entry = {
        "auth_config_id": auth_config_id,
        "auth_scheme": auth_scheme,
        "scopes": scope_list,
        "managed": False,
        "oauth_redirect_uri": resolved_redirect_uri,
        "display_name": display_name,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
    }
    overrides[key] = entry
    config[CUSTOM_AUTH_CONFIG_KEY] = overrides
    IntegrationProviderDAO(session).patch_backend(backend_id, {"config_json": config})
    session.commit()

    # Best-effort cleanup of a superseded custom config in the provider.
    if (
        old_auth_config_id
        and old_auth_config_id != auth_config_id
        and hasattr(adapter, "delete_auth_config")
    ):
        try:
            adapter.delete_auth_config(str(old_auth_config_id))
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to delete superseded Composio auth config %s for %s/%s",
                old_auth_config_id,
                backend_id,
                key,
            )
    return {"backend_id": backend_id, "toolkit_slug": key, **entry}


def delete_custom_auth_config(
    session: Session,
    *,
    backend_id: str,
    toolkit_slug: str,
    delete_remote: bool = True,
) -> None:
    """Remove an operator's bring-your-own OAuth config for a toolkit.

    Connect flows for the toolkit fall back to provider-managed auth afterwards
    (where available). Also deletes the config in the provider unless
    ``delete_remote`` is false.
    """

    backend = _require_backend(session, backend_id)
    config = dict(backend.config_json or {})
    overrides = dict(config.get(CUSTOM_AUTH_CONFIG_KEY) or {})
    key = (toolkit_slug or "").strip().upper()
    entry = overrides.pop(key, None)
    if entry is None:
        raise ValueError(
            f"No custom OAuth config for {toolkit_slug} on backend {backend_id}.",
        )
    config[CUSTOM_AUTH_CONFIG_KEY] = overrides
    IntegrationProviderDAO(session).patch_backend(backend_id, {"config_json": config})
    session.commit()

    if delete_remote and isinstance(entry, dict) and entry.get("auth_config_id"):
        adapter = get_provider_adapter(
            backend_id,
            backend_config=(backend.config_json or {}),
            backend_status=backend.status,
            require_live=True,
        )
        if hasattr(adapter, "delete_auth_config"):
            try:
                adapter.delete_auth_config(str(entry["auth_config_id"]))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to delete Composio auth config %s for %s/%s",
                    entry.get("auth_config_id"),
                    backend_id,
                    key,
                )


def _provider_link_alias(connection: Any) -> str:
    """The alias to hand the provider when creating a connected account.

    ``connection_id`` is Orchestra's stable identity: tasks and bindings
    store it, which is exactly why ``start_connection`` reuses the row
    rather than minting a new one. A provider alias is the opposite kind of
    name — Composio consumes it permanently when it creates an account, and
    refuses (bodyless HTTP 400) any later link that asks for the same one.

    Sending one as the other conflated the two lifecycles, and the failure
    was terminal rather than transient: once an account existed upstream,
    every later attempt on that row asked for a taken alias, and no amount
    of cancelling helped — cancel keeps ``connection_id`` by design, so the
    next attempt reproduced the collision exactly.

    So the alias stays ``connection_id`` while the row has never produced an
    upstream account — the common path, and stable across a double-submit,
    which is what stops a double-click minting two accounts. Once
    ``provider_connection_id`` is set that alias is spent, and a re-link
    gets a fresh one. ``connection_id`` never changes, so nothing that
    stored it is disturbed.
    """
    connection_id = str(getattr(connection, "connection_id", "") or "")
    if not getattr(connection, "provider_connection_id", None):
        return connection_id
    return f"{connection_id}-{uuid.uuid4().hex[:8]}"


def _provider_connect_url(
    *,
    backend: IntegrationBackend | None,
    app: Any | None = None,
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
            # An operator-configured bring-your-own OAuth config takes precedence
            # over Composio-managed auth (and is the only option for toolkits
            # Composio has no managed credentials for, e.g. TikTok).
            if not auth_config_id:
                auth_config_id = _custom_auth_config_id(
                    config,
                    connection.provider_app_id,
                )
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
                    app_label = (
                        app.display_name
                        if app and getattr(app, "display_name", None)
                        else connection.provider_app_id
                    )
                    if _is_missing_managed_auth_error(exc):
                        raise ProviderConnectError(
                            f"{app_label} has no managed OAuth credentials. An admin "
                            "must add a custom OAuth app for it in integration "
                            "settings before it can be connected.",
                            status_code=409,
                            code="custom_oauth_required",
                        ) from exc
                    raise ProviderConnectError(
                        f"Could not start the {app_label} connection: the provider "
                        "rejected the authorization request. Check the integration's "
                        "OAuth configuration and try again.",
                        status_code=502,
                        code="provider_auth_config_failed",
                    ) from exc
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
                    raise ProviderConnectError(
                        f"Could not start the {connection.provider_app_id} connection: "
                        "no OAuth configuration is available. An admin may need to add "
                        "a custom OAuth app for it in integration settings.",
                        status_code=409,
                        code="custom_oauth_required",
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
                    alias=_provider_link_alias(connection),
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
                # A provider that refused the authorization request is an
                # upstream failure, not a missing resource. Raising a bare
                # ValueError landed on the view's catch-all, which answers
                # 404 — so the browser was told "Not Found" for a Composio
                # 400, with no code to branch on and nothing in the message
                # a user could act on. Every sibling failure on this path
                # already raises ProviderConnectError; this one was the
                # exception.
                app_label = (
                    app.display_name
                    if app and getattr(app, "display_name", None)
                    else connection.provider_app_id
                )
                raise ProviderConnectError(
                    f"Could not start the {app_label} connection: the provider "
                    "rejected the authorization request. If a previous attempt "
                    "is still pending, cancel it and try again.",
                    status_code=502,
                    code="provider_auth_link_failed",
                )
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
                logger.warning(
                    "Pipedream connect link returned provider error backend_id=%s "
                    "provider_app_id=%s canonical_app_slug=%s connection_id=%s "
                    "owner_scope=%s error=%s",
                    connection.backend_id,
                    connection.provider_app_id,
                    app.canonical_app_slug if app else connection.canonical_app_slug,
                    connection.connection_id,
                    owner.owner_scope,
                    error,
                )
                raise ProviderConnectError(
                    f"Could not start the {connection.provider_app_id} connection: "
                    "the provider rejected the authorization request.",
                    status_code=502,
                    code="provider_connect_link_failed",
                )
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


def get_app_preference(
    session: Session,
    owner: OwnerContext,
    *,
    canonical_app_slug: str,
) -> IntegrationAppPreferenceResponse:
    seed_default_provider_catalog(session)
    preference = IntegrationProviderDAO(session).get_or_create_app_preference(
        owner=owner,
        canonical_app_slug=canonical_app_slug,
    )
    session.flush()
    return IntegrationAppPreferenceResponse(
        canonical_app_slug=preference.canonical_app_slug,
        owner_scope=preference.owner_scope,  # type: ignore[arg-type]
        org_id=preference.org_id,
        team_id=preference.team_id,
        user_id=preference.user_id,
        assistant_id=preference.assistant_id,
        usage_mode=preference.usage_mode,  # type: ignore[arg-type]
        pool_cursor=preference.pool_cursor,
        updated_at=preference.updated_at,
    )


def update_app_preference(
    session: Session,
    owner: OwnerContext,
    *,
    canonical_app_slug: str,
    usage_mode: str,
) -> IntegrationAppPreferenceResponse:
    seed_default_provider_catalog(session)
    preference = IntegrationProviderDAO(session).upsert_app_preference(
        owner=owner,
        canonical_app_slug=canonical_app_slug,
        usage_mode=usage_mode,
    )
    session.flush()
    return IntegrationAppPreferenceResponse(
        canonical_app_slug=preference.canonical_app_slug,
        owner_scope=preference.owner_scope,  # type: ignore[arg-type]
        org_id=preference.org_id,
        team_id=preference.team_id,
        user_id=preference.user_id,
        assistant_id=preference.assistant_id,
        usage_mode=preference.usage_mode,  # type: ignore[arg-type]
        pool_cursor=preference.pool_cursor,
        updated_at=preference.updated_at,
    )


def _best_connection(
    session: Session,
    *,
    owner: OwnerContext,
    canonical_app_slug: str,
    backend_id: str | None = None,
    connection_id: Optional[str] = None,
) -> Optional[IntegrationConnection]:
    conn = IntegrationProviderDAO(session).best_connection(
        owner=owner,
        canonical_app_slug=canonical_app_slug,
        backend_id=backend_id,
        connection_id=connection_id,
    )
    session.flush()
    return conn


def _active_backend_ids(session: Session) -> set[str]:
    seed_default_provider_catalog(session)
    return IntegrationProviderDAO(session).active_backend_ids()


def start_connection(
    session: Session,
    *,
    owner: OwnerContext,
    canonical_app_slug: str,
    backend_id: Optional[str],
    provider_app_id: Optional[str],
    requested_scopes: list[str],
    auth_mode: Optional[str],
    api_key_fields: dict[str, str],
    created_by: Optional[str],
    redirect_url: Optional[str],
    account_label: Optional[str] = None,
) -> tuple[IntegrationConnectionResponse, Optional[str], str, bool, list[str]]:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    resolved_backend_id = backend_id or "composio"
    resolved_provider_app_id = provider_app_id or canonical_app_slug
    backend = dao.get_backend(resolved_backend_id)
    if not backend or backend.status != "enabled":
        raise ValueError(f"Integration backend is disabled: {resolved_backend_id}")
    if resolved_backend_id == "unity_native":
        raise ValueError(
            "Native Unity-deploy integrations are deployment-enabled and do not create provider connections.",
        )

    chosen_auth_mode = auth_mode or "oauth"
    effective_requested_scopes = requested_scopes
    status = (
        "connected" if chosen_auth_mode == "api_key" and api_key_fields else "pending"
    )
    credential_storage = (
        "secret_manager" if chosen_auth_mode == "api_key" else "provider_vault"
    )
    baseline_completed = (
        _integration_onboarding_baseline(
            session,
            assistant_id=owner.assistant_id,
        )
        if status == "connected"
        else None
    )
    effective_user_id = owner.user_id
    if (
        owner.owner_scope == "assistant"
        and not effective_user_id
        and owner.assistant_id is not None
    ):
        from orchestra.db.models.orchestra_models import Assistant

        try:
            coordinator = session.get(Assistant, owner.assistant_id)
        except Exception:
            logger.exception(
                "assistant_user_id_lookup_failed assistant_id=%s",
                owner.assistant_id,
            )
            session.rollback()
            coordinator = None
        if coordinator is not None:
            effective_user_id = coordinator.user_id
    connection_values = {
        "owner_scope": owner.owner_scope,
        "org_id": owner.org_id,
        "team_id": owner.team_id,
        "user_id": effective_user_id,
        "assistant_id": owner.assistant_id,
        "canonical_app_slug": canonical_app_slug,
        "backend_id": resolved_backend_id,
        "provider_app_id": resolved_provider_app_id,
        "provider_connection_id": (
            f"local_{uuid.uuid4().hex}" if status == "connected" else None
        ),
        "status": status,
        "granted_scopes_json": effective_requested_scopes,
        "enabled_capabilities_json": [],
        "credential_storage": credential_storage,
        "secret_refs_json": {key: "<redacted>" for key in api_key_fields},
        "external_account_label": _normalize_account_label(account_label),
        "created_by": created_by,
        "reconnect_reason": (
            None if status == "connected" else "authorization_required"
        ),
    }
    existing_connection = dao.find_connection_for_owner_app(
        owner=replace(owner, user_id=effective_user_id),
        canonical_app_slug=canonical_app_slug,
    )

    def _label_key(label: Optional[str]) -> Optional[str]:
        normalized = _normalize_account_label(label)
        return normalized.lower() if normalized is not None else None

    # Reuse the existing row for a genuine reconnect of the *same* account
    # (not currently connected, or a same-account double-submit while already
    # connected). Compared case-insensitively since account labels are
    # typically emails and a caller may resend a differently-cased rendering
    # of the same account. A different account_label means the caller is
    # deliberately connecting a second account for this app_slug, which must
    # get its own row rather than overwrite the first one.
    reuse_existing = existing_connection is not None and (
        existing_connection.status != "connected"
        or _label_key(account_label)
        == _label_key(existing_connection.external_account_label)
    )
    if reuse_existing:
        connection = dao.update_connection_fields(
            existing_connection,
            **connection_values,
        )
    else:
        connection = dao.create_connection(connection_values)
    session.commit()

    connect_url = None
    if chosen_auth_mode == "oauth":
        connect_url = _provider_connect_url(
            backend=backend,
            app=None,
            owner=owner,
            connection=connection,
            redirect_url=redirect_url,
        )
        session.commit()
    if status == "connected":
        logger.info(
            "onboarding_integration_mutation connection_id=%s assistant_id=%s status=connected canonical_app_slug=%s",
            connection.connection_id,
            connection.assistant_id,
            connection.canonical_app_slug,
        )
        _notify_integration_onboarding_connected(
            session,
            assistant_id=connection.assistant_id,
            canonical_app_slug=connection.canonical_app_slug,
            baseline_completed_step_ids=baseline_completed,
            connection_id=connection.connection_id,
        )
    return (
        _connection_to_response(connection),
        connect_url,
        chosen_auth_mode,
        chosen_auth_mode == "oauth",
        effective_requested_scopes,
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
    baseline_completed = (
        _integration_onboarding_baseline(
            session,
            assistant_id=conn.assistant_id,
            connection_id=connection_id,
        )
        if status == "connected"
        else None
    )
    # Re-load after a possible baseline rollback so the update runs on a
    # clean session (baseline failures must not strand OAuth completion).
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    resolved_provider_connection_id = (
        provider_connection_id
        or conn.provider_connection_id
        or f"local_{uuid.uuid4().hex}"
    )
    updates = {
        "provider_connection_id": resolved_provider_connection_id,
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
    if status == "connected" and not resolved_provider_connection_id.startswith(
        "local_",
    ):
        # Persist the entity id the provider bound this account to at link
        # time: execution must present that exact id, and the connection's
        # owner scope can legitimately diverge from it later (ownership
        # transfers, mirrored environments).
        try:
            adapter = get_provider_adapter(conn.backend_id)
            provider_user_id = adapter.connected_account_user_id(
                resolved_provider_connection_id,
            )
        except Exception:
            logger.exception(
                "Failed to resolve provider entity id for connection %s",
                conn.connection_id,
            )
            provider_user_id = None
        if provider_user_id:
            updates["provider_user_id"] = provider_user_id
    dao.update_connection_fields(conn, **updates)
    session.commit()
    response = _connection_to_response(conn)
    logger.info(
        "onboarding_integration_mutation connection_id=%s assistant_id=%s status=%s canonical_app_slug=%s",
        conn.connection_id,
        conn.assistant_id,
        status,
        conn.canonical_app_slug,
    )
    if status == "connected":
        _notify_integration_onboarding_connected(
            session,
            assistant_id=conn.assistant_id,
            canonical_app_slug=conn.canonical_app_slug,
            baseline_completed_step_ids=baseline_completed,
            connection_id=conn.connection_id,
        )
    return response


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


def _release_provider_account(session: Session, conn: Any, *, reason: str) -> None:
    """Delete the upstream account this row points at, if the backend has one.

    Disconnecting used to write our own row and stop there, so every
    disconnect left a live account behind at the provider: its OAuth grant
    intact, counted against the workspace, and — while ACTIVE — still
    reserving its alias, which is enough to refuse the reconnect meant to
    replace it.

    Best effort by design. The user asked to disconnect; a provider that is
    down or has already removed the account must not keep our row connected.
    A failure is logged with the ids needed to sweep it later.
    """
    provider_connection_id = getattr(conn, "provider_connection_id", None)
    if not provider_connection_id:
        return
    dao = IntegrationProviderDAO(session)
    backend = dao.get_backend(conn.backend_id)
    try:
        adapter = get_provider_adapter(
            conn.backend_id,
            backend_config=(backend.config_json if backend else {}),
            backend_status=backend.status if backend else "enabled",
        )
        deleter = getattr(adapter, "delete_connected_account", None)
        if deleter is None:
            return
        deleter(provider_connection_id)
    except Exception:
        logger.exception(
            "Failed to delete upstream connected account reason=%s backend_id=%s "
            "connection_id=%s provider_connection_id=%s canonical_app_slug=%s",
            reason,
            conn.backend_id,
            conn.connection_id,
            provider_connection_id,
            conn.canonical_app_slug,
        )


def disconnect_connection(
    session: Session,
    connection_id: str,
) -> IntegrationConnectionResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    baseline_completed = _integration_onboarding_baseline(
        session,
        assistant_id=conn.assistant_id,
        connection_id=connection_id,
    )
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    _release_provider_account(session, conn, reason="user_disconnected")
    dao.update_connection_fields(
        conn,
        status="disconnected",
        reconnect_reason="user_disconnected",
        provider_connection_id=None,
    )
    session.commit()
    response = _connection_to_response(conn)
    logger.info(
        "onboarding_integration_mutation connection_id=%s assistant_id=%s status=disconnected canonical_app_slug=%s",
        conn.connection_id,
        conn.assistant_id,
        conn.canonical_app_slug,
    )
    _notify_integration_onboarding_disconnected(
        session,
        assistant_id=conn.assistant_id,
        canonical_app_slug=conn.canonical_app_slug,
        baseline_completed_step_ids=baseline_completed,
        connection_id=conn.connection_id,
    )
    return response


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
    _release_provider_account(session, conn, reason="setup_cancelled")
    dao.update_connection_fields(
        conn,
        status="disconnected",
        reconnect_reason="setup_cancelled",
        provider_connection_id=None,
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
    owner: OwnerContext | None = None,
) -> IntegrationToolPolicyResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    _assert_connection_owner(dao, conn, owner)
    policy = _connection_tool_policy(conn)
    return IntegrationToolPolicyResponse(
        connection_id=conn.connection_id,
        canonical_app_slug=conn.canonical_app_slug,
        app_display_name=conn.canonical_app_slug.replace("_", " ").title(),
        account_label=conn.external_account_label,
        policies=[
            IntegrationToolPolicyItem(
                tool_id=tool_id,
                provider_tool_id=tool_id,
                canonical_name=tool_id,
                display_name=tool_id.rsplit(":", 1)[-1].replace("_", " ").title(),
                action_class="write",
                behavior_hints=[],
                default_approval_level="specific_approval",
                approval_level=approval_level,
                activation_state="connected_ready",
                confirmation_required=approval_level == "specific_approval",
            )
            for tool_id, approval_level in sorted(policy.items())
        ],
    )


def patch_connection_tool_policy(
    session: Session,
    connection_id: str,
    body: IntegrationToolPolicyPatchRequest,
    owner: OwnerContext | None = None,
) -> IntegrationToolPolicyResponse:
    dao = IntegrationProviderDAO(session)
    conn = dao.get_connection(connection_id)
    if not conn:
        raise ValueError(f"Unknown connection: {connection_id}")
    _assert_connection_owner(dao, conn, owner)
    policy = {} if body.reset_to_defaults else dict(_connection_tool_policy(conn))
    if body.bulk_approval_level:
        for tool_id in list(policy):
            policy[tool_id] = body.bulk_approval_level
    for tool_id, approval_level in body.tool_policies.items():
        policy[tool_id] = approval_level

    dao.set_connection_tool_policy(conn, policy)
    session.commit()
    return get_connection_tool_policy(session, connection_id, owner=owner)


def _set_policy_for_approval_scope(
    *,
    dao: IntegrationProviderDAO,
    conn: IntegrationConnection,
    audit: ProviderActionAudit,
    scope: str,
    approval_level: str,
) -> bool:
    policy = dict(_connection_tool_policy(conn))
    if scope == "tool":
        if not audit.tool_id:
            return False
        policy[audit.tool_id] = approval_level
    elif scope == "app_action_class":
        if not audit.tool_id:
            return False
        policy[audit.tool_id] = approval_level
    else:
        return False
    dao.set_connection_tool_policy(conn, policy)
    return True


def _approval_request_owner(
    body: IntegrationToolExecutionApprovalRequest,
) -> OwnerContext:
    return OwnerContext(
        owner_scope=body.owner_scope,
        org_id=body.org_id,
        team_id=body.team_id,
        user_id=body.user_id,
        assistant_id=body.assistant_id,
    )


def approve_tool_execution(
    session: Session,
    audit_id: int,
    body: IntegrationToolExecutionApprovalRequest,
) -> IntegrationToolExecutionApprovalResponse:
    dao = IntegrationProviderDAO(session)
    audit = dao.get_action_audit(audit_id)
    if not audit:
        raise ValueError(f"Unknown provider action audit: {audit_id}")
    _assert_audit_owner(audit, _approval_request_owner(body))
    conn = dao.get_connection(audit.connection_id) if audit.connection_id else None
    if conn:
        _assert_connection_owner(dao, conn, _approval_request_owner(body))
    if audit.expires_at:
        expires_at = audit.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            dao.update_action_audit(audit, status="expired")
            session.commit()
            return IntegrationToolExecutionApprovalResponse(
                status="expired",
                audit_id=audit.id,
                connection_id=audit.connection_id,
                tool_id=audit.tool_id,
                approval_scope=body.scope,
                approval_level=body.approval_level,
                expires_at=audit.expires_at,
                policy_updated=False,
            )

    policy_updated = False
    if body.persist_policy and conn:
        policy_updated = _set_policy_for_approval_scope(
            dao=dao,
            conn=conn,
            audit=audit,
            scope=body.scope,
            approval_level=body.approval_level,
        )

    expires_at = body.expires_at or audit.expires_at or _confirmation_expires_at()
    confirmation_token = (
        create_confirmation_token(
            tool_id=audit.tool_id,
            connection_id=audit.connection_id,
            ttl_seconds=_confirmation_ttl_seconds(),
        )
        if audit.tool_id and audit.connection_id
        else None
    )
    dao.update_action_audit(
        audit,
        status="approved",
        approval_scope=body.scope,
        approval_level=body.approval_level,
        approved_by=body.actor_id,
        approved_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    )
    session.commit()
    return IntegrationToolExecutionApprovalResponse(
        status="approved",
        audit_id=audit.id,
        connection_id=audit.connection_id,
        tool_id=audit.tool_id,
        approval_scope=body.scope,
        approval_level=body.approval_level,
        confirmation_token=confirmation_token,
        expires_at=expires_at,
        policy_updated=policy_updated,
    )


def deny_tool_execution(
    session: Session,
    audit_id: int,
    body: IntegrationToolExecutionApprovalRequest,
) -> IntegrationToolExecutionApprovalResponse:
    dao = IntegrationProviderDAO(session)
    audit = dao.get_action_audit(audit_id)
    if not audit:
        raise ValueError(f"Unknown provider action audit: {audit_id}")
    _assert_audit_owner(audit, _approval_request_owner(body))
    conn = dao.get_connection(audit.connection_id) if audit.connection_id else None
    if conn:
        _assert_connection_owner(dao, conn, _approval_request_owner(body))
    policy_updated = False
    if body.persist_policy and conn:
        policy_updated = _set_policy_for_approval_scope(
            dao=dao,
            conn=conn,
            audit=audit,
            scope=body.scope if body.scope != "once" else "tool",
            approval_level="forbidden",
        )
    dao.update_action_audit(
        audit,
        status="denied",
        approval_scope=body.scope,
        approval_level="forbidden",
        denied_by=body.actor_id,
        denied_at=datetime.now(timezone.utc),
        error_code=body.reason or "user_denied",
    )
    session.commit()
    return IntegrationToolExecutionApprovalResponse(
        status="denied",
        audit_id=audit.id,
        connection_id=audit.connection_id,
        tool_id=audit.tool_id,
        approval_scope=body.scope,
        approval_level="forbidden",
        expires_at=audit.expires_at,
        policy_updated=policy_updated,
    )


def _activation_state(
    tool: RuntimeProviderTool,
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
    return "connected_ready"


def _policy_error(
    *,
    backend: IntegrationBackend | None,
    overlay: IntegrationOverlay | None,
    tool: RuntimeProviderTool,
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
    tool: RuntimeProviderTool,
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


def _redact_summary(payload: dict[str, Any]) -> str:
    keys = sorted(payload.keys())
    return f"keys={keys[:20]}"


def _arguments_hash(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        arguments or {},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _arguments_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    keys = sorted((arguments or {}).keys())
    return {
        "keys": keys[:20],
        "total_keys": len(keys),
    }


def _provider_status_code(error: dict[str, Any] | None) -> int | None:
    if not error:
        return None
    value = error.get("provider_status_code")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _provider_request_summary(error: dict[str, Any] | None) -> dict[str, Any]:
    if not error:
        return {}
    summary = error.get("provider_request")
    return summary if isinstance(summary, dict) else {}


def _runtime_tool_from_request(
    tool_id: str,
    body: ProviderToolRunRequest,
) -> RuntimeProviderTool:
    parts = tool_id.split(":", 2)
    backend_id = body.backend_id or (parts[0] if len(parts) == 3 else "composio")
    app_slug = (
        body.app_slug
        or body.canonical_app_slug
        or (parts[1] if len(parts) == 3 else "unknown")
    )
    name = parts[2] if len(parts) == 3 else tool_id
    provider_tool_id = body.provider_tool_id or f"{app_slug}.{name}"
    canonical_name = body.canonical_name or f"primitives.integrations.{app_slug}.{name}"
    return RuntimeProviderTool(
        tool_id=tool_id,
        backend_id=backend_id,
        provider_app_id=body.provider_app_id or app_slug,
        canonical_app_slug=app_slug,
        app_display_name=body.app_display_name or app_slug.replace("_", " ").title(),
        app_icon_url=body.app_icon_url,
        provider_tool_id=provider_tool_id,
        unify_tool_id=canonical_name,
        canonical_name=canonical_name,
        function_manager_name=body.function_manager_name
        or f"primitives_integrations__{app_slug}__{name}",
        name=name,
        display_name=body.tool_display_name or name.replace("_", " ").title(),
        description=body.tool_display_name or name.replace("_", " ").title(),
        action_class=body.action_class or "write",
        behavior_hints_json=body.behavior_hints,
        required_scopes_json=body.required_scopes,
        input_schema_json=body.input_schema or {},
        output_schema_json=body.output_schema or {},
        examples_json=body.examples,
        confirmation_required=body.confirmation_required,
    )


def _confirmation_ttl_seconds() -> int:
    try:
        return int(os.getenv("INTEGRATION_CONFIRMATION_TTL_SECONDS", "900"))
    except ValueError:
        return 900


def _confirmation_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=_confirmation_ttl_seconds())


def _approval_options(tool: RuntimeProviderTool) -> list[str]:
    options = ["once", "tool"]
    if tool.action_class in {"read", "sensitive_read"}:
        options.append("app_action_class")
    return options


def _build_confirmation_payload(
    *,
    audit: ProviderActionAudit,
    tool: RuntimeProviderTool,
    app: Any | None,
    conn: IntegrationConnection | None,
    confirmation_token: str | None,
) -> ProviderToolConfirmationPayload:
    return ProviderToolConfirmationPayload(
        audit_id=audit.id,
        connection_id=conn.connection_id if conn else None,
        tool_id=tool.tool_id,
        app_slug=tool.canonical_app_slug,
        app_display_name=app.display_name if app else tool.app_display_name,
        account_label=conn.external_account_label if conn else None,
        tool_display_name=tool.display_name,
        action_class=tool.action_class,
        behavior_hints=tool.behavior_hints_json or [],
        arguments_summary=audit.arguments_summary_json or {},
        approval_level=_effective_tool_policy_level(tool, conn),
        approval_options=_approval_options(tool),
        confirmation_token=confirmation_token,
        expires_at=audit.expires_at,
    )


@dataclass(frozen=True)
class ApprovalResolution:
    decision: str
    audit: ProviderActionAudit | None = None
    confirmation: ProviderToolConfirmationPayload | None = None
    error: dict[str, Any] | None = None


def _audit_values(
    *,
    body: ProviderToolRunRequest,
    tool: RuntimeProviderTool,
    conn: IntegrationConnection | None,
    status: str,
    start: float,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "org_id": body.org_id,
        "team_id": body.team_id,
        "user_id": body.user_id,
        "assistant_id": body.assistant_id,
        "conversation_id": body.conversation_id,
        "connection_id": conn.connection_id if conn else None,
        "provider_connection_id": conn.provider_connection_id if conn else None,
        "backend_id": tool.backend_id,
        "canonical_app_slug": tool.canonical_app_slug,
        "tool_id": tool.tool_id,
        "provider_action_id": tool.provider_tool_id,
        "provider_tool_id": tool.provider_tool_id,
        "unify_tool_id": tool.unify_tool_id,
        "action_class": tool.action_class,
        "behavior_hints_json": tool.behavior_hints_json or [],
        "status": status,
        "latency_ms": int((time.monotonic() - start) * 1000),
        "arguments_hash": _arguments_hash(body.arguments),
        "arguments_summary_json": _arguments_summary(body.arguments),
        "redacted_input_summary": _redact_summary(body.arguments),
        "redacted_output_summary": _redact_summary(result or {}) if result else None,
        "error_code": error.get("code") if error else None,
        "provider_status_code": _provider_status_code(error),
        "provider_response_body": (error or {}).get("provider_response_body"),
        "provider_request_summary_json": _provider_request_summary(error),
        "expires_at": expires_at,
    }


def _approved_audit_matches(
    *,
    audit: ProviderActionAudit,
    tool: RuntimeProviderTool,
    conn: IntegrationConnection | None,
    arguments_hash: str,
) -> bool:
    if audit.status != "approved":
        return False
    if audit.tool_id and audit.tool_id != tool.tool_id:
        return False
    if audit.connection_id != (conn.connection_id if conn else None):
        return False
    if audit.arguments_hash and audit.arguments_hash != arguments_hash:
        return False
    expires_at = audit.expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at < datetime.now(timezone.utc):
        return False
    return True


def _resolve_tool_approval(
    *,
    dao: IntegrationProviderDAO,
    body: ProviderToolRunRequest,
    tool: RuntimeProviderTool,
    conn: IntegrationConnection | None,
    overlay: IntegrationOverlay | None,
    start: float,
) -> ApprovalResolution:
    if not _tool_requires_confirmation(tool, conn, overlay):
        return ApprovalResolution(decision="auto")
    arguments_hash = _arguments_hash(body.arguments)
    if body.approval_audit_id is not None:
        approved_audit = dao.get_action_audit(body.approval_audit_id)
        if approved_audit and _approved_audit_matches(
            audit=approved_audit,
            tool=tool,
            conn=conn,
            arguments_hash=arguments_hash,
        ):
            dao.update_action_audit(
                approved_audit,
                status="executing",
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            return ApprovalResolution(decision="approved", audit=approved_audit)
        return ApprovalResolution(
            decision="needs_confirmation",
            error={
                "code": "invalid_approval",
                "message": "Approval is invalid, expired, or does not match this tool call.",
            },
        )
    if _valid_confirmation_token(
        body.confirmation_token,
        tool_id=tool.tool_id,
        connection_id=conn.connection_id if conn else None,
    ):
        return ApprovalResolution(decision="approved")

    expires_at = _confirmation_expires_at()
    error = {
        "code": "confirmation_required",
        "message": "This action requires explicit confirmation before execution.",
    }
    audit = dao.add_action_audit(
        _audit_values(
            body=body,
            tool=tool,
            conn=conn,
            status="pending_confirmation",
            start=start,
            error=error,
            expires_at=expires_at,
        ),
    )
    confirmation_token = (
        create_confirmation_token(
            tool_id=tool.tool_id,
            connection_id=conn.connection_id,
            ttl_seconds=_confirmation_ttl_seconds(),
        )
        if conn
        else None
    )
    return ApprovalResolution(
        decision="needs_confirmation",
        audit=audit,
        confirmation=_build_confirmation_payload(
            audit=audit,
            tool=tool,
            app=None,
            conn=conn,
            confirmation_token=confirmation_token,
        ),
        error=error,
    )


def _provider_error_outcome(
    error: dict[str, Any] | None,
    *,
    required_scopes: list[str],
) -> tuple[str, str | None, dict[str, Any]]:
    """Map a provider execution error onto a run outcome.

    The provider is the single source of truth for whether a connection may run
    an action. Authorization failures are surfaced as actionable reconnect
    outcomes derived from the provider's own response, never from a locally
    predicted scope comparison. Returns ``(status, activation_state_override,
    error)`` where the override is ``None`` when the local activation state
    should stand.
    """

    error = error or {"code": "provider_error", "message": "Provider execution failed."}
    provider_status_code = error.get("provider_status_code")
    if provider_status_code == 403:
        return (
            "missing_scope",
            "missing_scope",
            {
                "code": "missing_scope",
                "message": (
                    "The connected account is missing the permissions required "
                    "for this action. Reconnect the integration to grant them."
                ),
                "required_scopes": required_scopes,
                "provider_status_code": provider_status_code,
                "provider_response_body": error.get("provider_response_body"),
            },
        )
    if provider_status_code == 401:
        return (
            "reconnect_required",
            "expired",
            {
                "code": "reconnect_required",
                "message": (
                    "The connected account is no longer authenticated. "
                    "Reconnect the integration."
                ),
                "provider_status_code": provider_status_code,
                "provider_response_body": error.get("provider_response_body"),
            },
        )
    return ("provider_error", None, error)


def _provider_retry_max_attempts() -> int:
    """Max provider adapter attempts per ``run_tool`` (opt out with ``1``)."""

    raw = os.environ.get("ORCHESTRA_INTEGRATION_RETRY_MAX_ATTEMPTS")
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_ATTEMPTS
    return max(1, int(raw))


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
    tool = _runtime_tool_from_request(tool_id, body)
    backend = dao.get_backend(tool.backend_id)
    overlay = dao.get_overlay(tool.canonical_app_slug)
    conn = _best_connection(
        session,
        owner=owner,
        canonical_app_slug=tool.canonical_app_slug,
        backend_id=tool.backend_id,
        connection_id=body.connection_id,
    )
    preference = dao.get_or_create_app_preference(
        owner=owner,
        canonical_app_slug=tool.canonical_app_slug,
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
    confirmation: ProviderToolConfirmationPayload | None = None
    audit: ProviderActionAudit | None = None
    provider_attempts = 1
    if preference.usage_mode == "explicit" and not body.connection_id and conn is None:
        status = "connection_id_required"
        error = {
            "code": "connection_id_required",
            "message": (
                f"{tool.canonical_app_slug} is configured for explicit account "
                "selection. Pass connection_id from list_connected."
            ),
        }
    elif policy_error:
        status = "blocked_by_policy"
        error = policy_error
    elif activation_state == "not_connected":
        status = "connect_required"
        error = {
            "code": "connect_required",
            "message": f"Connect {tool.canonical_app_slug} in Console before using this tool.",
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
    else:
        approval = _resolve_tool_approval(
            dao=dao,
            body=body,
            tool=tool,
            conn=conn,
            overlay=overlay,
            start=start,
        )
        audit = approval.audit
        if approval.decision == "needs_confirmation":
            status = "confirmation_required"
            error = approval.error
            confirmation = approval.confirmation
        else:
            adapter = get_provider_adapter(
                tool.backend_id,
                backend_config=(backend.config_json if backend else {}),
                backend_status=backend.status if backend else "enabled",
            )
            # The entity id recorded at link time is authoritative: providers
            # (Composio) reject executions whose user_id differs from the one
            # the account was created under, and the connection's owner scope
            # can diverge from it after ownership transfers or mirroring.
            provider_user_id = (
                conn.provider_user_id if conn else None
            ) or _owner_external_user_id(
                owner,
                conn.connection_id if conn else None,
            )
            execution_request = ProviderExecutionRequest(
                backend_id=tool.backend_id,
                tool_id=tool.tool_id,
                canonical_app_slug=tool.canonical_app_slug,
                provider_tool_id=tool.provider_tool_id,
                connection_id=conn.connection_id if conn else None,
                provider_connection_id=(conn.provider_connection_id if conn else None),
                action_class=tool.action_class,
                user_id=provider_user_id,
                arguments=body.arguments,
            )
            max_attempts = _provider_retry_max_attempts()

            def _should_retry(adapter_result: Any) -> tuple[bool, str | None]:
                return should_retry_adapter_result(
                    action_class=tool.action_class,
                    status=adapter_result.status,
                    error=adapter_result.error,
                    result=adapter_result.result,
                )

            def _on_retry(attempt: int, reason: str | None, delay: float) -> None:
                logger.info(
                    "Retrying provider tool %s (attempt %s/%s, reason=%s, "
                    "sleep=%.2fs)",
                    tool.tool_id,
                    attempt,
                    max_attempts,
                    reason,
                    delay,
                )

            adapter_result, provider_attempts = call_with_transient_retries(
                lambda: adapter.execute(execution_request),
                should_retry=_should_retry,
                max_attempts=max_attempts,
                on_retry=_on_retry,
                retry_after_from_value=lambda ar: parse_retry_after_seconds(ar.error),
            )
            if adapter_result.status == "ok":
                result = adapter_result.result
                # After exhausting retries, surface persistent embedded platform
                # failures (GraphQL / Pipedream action errors) as provider_error
                # rather than a misleading ok envelope.
                if is_safe_to_retry_ok_result(action_class=tool.action_class):
                    reason, details = embedded_transient_ok_failures(result)
                    if reason:
                        status = "provider_error"
                        error = {
                            "code": "provider_error",
                            "message": (
                                f"Provider {reason} persisted after "
                                f"{provider_attempts} attempt(s): {details}"
                            ),
                            "embedded_failure_reason": reason,
                            "embedded_failure_details": details,
                        }
                        result = {}
            else:
                status, activation_override, error = _provider_error_outcome(
                    adapter_result.error,
                    required_scopes=tool.required_scopes_json or [],
                )
                if activation_override:
                    activation_state = activation_override

    audit_values = _audit_values(
        body=body,
        tool=tool,
        conn=conn,
        status="pending_confirmation" if status == "confirmation_required" else status,
        start=start,
        result=result,
        error=error,
        expires_at=audit.expires_at if audit else None,
    )
    if audit:
        dao.update_action_audit(audit, **audit_values)
    else:
        audit = dao.add_action_audit(audit_values)
    session.commit()

    return ProviderToolRunResponse(
        status=status,
        activation_state=activation_state,
        tool_id=tool.tool_id,
        connection_id=conn.connection_id if conn else None,
        result=result,
        error=error,
        audit_id=audit.id,
        confirmation=confirmation,
        provider_attempts=provider_attempts,
    )


def stage_composio_file(
    *,
    content: bytes,
    filename: str,
    mimetype: str,
    toolkit_slug: str,
    tool_slug: str,
) -> dict[str, Any]:
    """Stage a file in Composio storage for FileUploadable tool parameters."""
    return stage_provider_file(
        backend_id="composio",
        content=content,
        filename=filename,
        mimetype=mimetype,
        toolkit_slug=toolkit_slug,
        tool_slug=tool_slug,
    )


def stage_provider_file(
    *,
    backend_id: str,
    content: bytes,
    filename: str,
    mimetype: str,
    toolkit_slug: str,
    tool_slug: str,
) -> dict[str, Any]:
    """Stage a file through the selected provider adapter."""

    from orchestra.integrations.providers.registry import get_provider_adapter

    adapter = get_provider_adapter(backend_id.strip().lower())
    normalized_tool_slug = tool_slug.strip()
    if backend_id.strip().lower() == "composio":
        normalized_tool_slug = normalized_tool_slug.upper()
    result = adapter.stage_file(
        content=content,
        filename=filename,
        mimetype=mimetype,
        toolkit_slug=toolkit_slug.strip().lower(),
        tool_slug=normalized_tool_slug,
    )
    if result.get("status") != "ok":
        return result
    raw_file = result.get("file") or {}
    if not isinstance(raw_file, dict):
        return {
            "status": "error",
            "error": {
                "code": "provider_file_stage_failed",
                "message": "Provider file staging returned an invalid payload.",
            },
        }
    return {
        "status": "ok",
        "backend_id": backend_id.strip().lower(),
        "file": {
            "name": str(raw_file.get("name") or filename),
            "mimetype": str(raw_file.get("mimetype") or mimetype),
            "s3key": raw_file.get("s3key"),
            "file_path": raw_file.get("file_path") or raw_file.get("filePath"),
            "stash_id": raw_file.get("stash_id") or raw_file.get("stashId"),
            "provider_metadata": {
                key: value
                for key, value in raw_file.items()
                if key
                not in {
                    "name",
                    "mimetype",
                    "s3key",
                    "file_path",
                    "filePath",
                    "stash_id",
                    "stashId",
                }
            },
        },
    }


def download_provider_file(*, backend_id: str, s3_key: str) -> dict[str, Any]:
    """Download a provider-staged file through the selected adapter."""

    from orchestra.integrations.providers.registry import get_provider_adapter

    adapter = get_provider_adapter(backend_id.strip().lower())
    result = adapter.download_file(s3_key=s3_key)
    if result.get("status") != "ok":
        return {**result, "backend_id": backend_id.strip().lower()}
    return {
        "status": "ok",
        "backend_id": backend_id.strip().lower(),
        "filename": result.get("filename"),
        "mimetype": result.get("mimetype"),
        "content_base64": result.get("content_base64"),
        "provider_metadata": result.get("provider_metadata") or {},
    }
