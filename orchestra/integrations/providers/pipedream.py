"""Pipedream integration provider adapter.

This module owns all Pipedream wire-format knowledge: HTTP transport plus the
normalization of Pipedream app/component payloads into Orchestra's canonical,
provider-neutral catalog entries (``ProviderAppEntry`` / ``ProviderToolEntry``).
The generic sync layer only calls ``list_app_entries`` / ``list_tool_entries``
and never imports any of the ``_pipedream_*`` helpers below.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from orchestra.integrations.providers.base import (
    BaseIntegrationProviderAdapter,
    ProviderAppEntry,
    ProviderExecutionRequest,
    ProviderExecutionResult,
    ProviderToolEntry,
)
from orchestra.integrations.providers.utils.normalization import (
    action_class_from_behavior_hints,
    confirmation_required_for_action_class,
    normalized_behavior_hints,
    slugify,
)
from orchestra.integrations.providers.utils.pagination import (
    CursorPage,
    PaginationLimits,
    collect_cursor_pages,
    int_or_none,
)


class PipedreamProviderAdapter(BaseIntegrationProviderAdapter):
    """HTTP adapter for Pipedream Connect catalog sync and action execution."""

    backend_id = "pipedream"
    MAX_PAGE_SIZE = 100

    def __init__(
        self,
        *,
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        project_id: str | None = None,
        environment: str | None = None,
        base_url: str | None = None,
        rest_base_url: str | None = None,
        oauth_token_url: str | None = None,
        oauth_scope: str | None = None,
        auth_prop_name: str | None = None,
        timeout_seconds: int = 30,
        max_pages: int = 1000,
        max_items: int = 100_000,
    ) -> None:
        super().__init__()
        self._app_by_canonical_slug: dict[str, dict[str, Any]] = {}
        self.access_token = access_token or os.getenv("PIPEDREAM_ACCESS_TOKEN")
        self.client_id = client_id or os.getenv("PIPEDREAM_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("PIPEDREAM_CLIENT_SECRET")
        self.project_id = project_id or os.getenv("PIPEDREAM_PROJECT_ID")
        self.environment = (
            environment or os.getenv("PIPEDREAM_ENVIRONMENT") or "production"
        )
        self.base_url = (base_url or "https://api.pipedream.com/v1/connect").rstrip("/")
        self.rest_base_url = (rest_base_url or "https://api.pipedream.com/v1").rstrip(
            "/",
        )
        self.oauth_token_url = (
            oauth_token_url
            or os.getenv("PIPEDREAM_OAUTH_TOKEN_URL")
            or "https://api.pipedream.com/v1/oauth/token"
        )
        self.oauth_scope = oauth_scope or os.getenv("PIPEDREAM_OAUTH_SCOPE")
        self.auth_prop_name = auth_prop_name
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.max_items = max_items

    def iter_apps(
        self,
        *,
        limit: int | None = None,
        search: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        return iter(self.list_apps(limit=limit, query=search))

    def iter_tools(
        self,
        *,
        app_id: str,
        limit: int | None = None,
    ) -> Iterable[dict[str, Any]]:
        return iter(
            self.list_components(app=app_id, limit=limit, component_type="action"),
        )

    def list_app_entries(
        self,
        *,
        app_slugs: list[str] | None = None,
        include_all: bool = False,
        create_auth_configs: bool = False,
        include_detail: bool = True,
    ) -> list[ProviderAppEntry]:
        """Select Pipedream apps (those exposing components) and emit canonical entries.

        Pipedream Connect manages OAuth itself, so there is no per-app auth-config
        creation, OAuth scope list, or API-key schema to populate here;
        ``include_detail`` and ``create_auth_configs`` are accepted for contract
        parity but have no extra detail fetch to perform.
        """

        self._reset_catalog_accounting()
        requested = {slugify(slug) for slug in (app_slugs or []) if slug.strip()}
        self.last_requested_app_slugs = sorted(requested)
        provider_apps = self.list_apps(has_components=True)
        self._app_by_canonical_slug = {}
        entries: list[ProviderAppEntry] = []
        for provider_app in provider_apps:
            canonical_app_slug = _pipedream_app_slug(provider_app)
            self._app_by_canonical_slug[canonical_app_slug] = provider_app
            if requested:
                if canonical_app_slug not in requested:
                    continue
            elif not include_all:
                continue
            provider_app_id = str(
                provider_app.get("id")
                or provider_app.get("name_slug")
                or provider_app.get("slug")
                or canonical_app_slug,
            )
            entries.append(
                {
                    "backend_id": "pipedream",
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
                    "tool_count": int(
                        provider_app.get("component_count")
                        or provider_app.get("actions_count")
                        or 0,
                    ),
                    "raw_provider_metadata": {
                        "source": "pipedream_live_sync",
                        "raw_app": provider_app,
                    },
                },
            )
        self.last_skipped_apps = [
            {"slug": slug, "reason": "not_found"}
            for slug in self.last_requested_app_slugs
            if slug not in self._app_by_canonical_slug
        ]
        return entries

    def list_tool_entries(
        self,
        *,
        app_slug: str,
        provider_app_id: str | None = None,
        limit: int | None = None,
    ) -> list[ProviderToolEntry]:
        """Fetch one app's public action components and emit canonical tool entries."""

        canonical_app_slug = slugify(app_slug)
        provider_app = self._app_by_canonical_slug.get(canonical_app_slug, {})
        resolved_app_id = str(
            provider_app_id
            or provider_app.get("id")
            or provider_app.get("name_slug")
            or provider_app.get("slug")
            or canonical_app_slug,
        )
        category = _pipedream_category(provider_app)
        entries: list[ProviderToolEntry] = []
        for component in self.list_components(
            app=resolved_app_id,
            limit=limit,
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
            behavior_hints = _pipedream_behavior_hints(component)
            action_class = action_class_from_behavior_hints(behavior_hints)
            entries.append(
                {
                    "backend_id": "pipedream",
                    "provider_app_id": resolved_app_id,
                    "canonical_app_slug": canonical_app_slug,
                    "provider_tool_id": provider_tool_id,
                    "name": tool_name,
                    "display_name": component.get("name")
                    or tool_name.replace("_", " ").title(),
                    "description": component.get("description")
                    or component.get("name")
                    or tool_name.replace("_", " ").title(),
                    "required_scopes": [],
                    "input_schema": _pipedream_input_schema(component),
                    "output_schema": {"type": "object"},
                    "action_class": action_class,
                    "behavior_hints": behavior_hints,
                    "confirmation_required": confirmation_required_for_action_class(
                        action_class,
                    ),
                    "category": category,
                    "tags": [canonical_app_slug, *([category] if category else [])],
                    "raw_provider_metadata": {
                        "source": "pipedream_live_sync",
                        "raw_component": component,
                    },
                },
            )
        return entries

    def list_apps(
        self,
        *,
        limit: int | None = None,
        query: str | None = None,
        has_components: bool | None = None,
        has_actions: bool | None = None,
        has_triggers: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch Pipedream apps using documented after/page_info pagination."""

        access_token, token_error = self._access_token()
        if token_error:
            raise ValueError(token_error["message"])

        import requests

        max_items = min(limit, self.max_items) if limit is not None else self.max_items
        page_size = min(limit or self.MAX_PAGE_SIZE, self.MAX_PAGE_SIZE)

        def fetch_page(cursor: str | None, page_size: int) -> CursorPage:
            params: dict[str, Any] = {"limit": page_size}
            if cursor:
                params["after"] = cursor
            if query:
                params["q"] = query
            if has_components is not None:
                params["has_components"] = has_components
            if has_actions is not None:
                params["has_actions"] = has_actions
            if has_triggers is not None:
                params["has_triggers"] = has_triggers
            response = requests.get(
                f"{self.rest_base_url}/apps",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return _pipedream_cursor_page(data)

        items = collect_cursor_pages(
            fetch_page,
            limits=PaginationLimits(
                page_size=page_size,
                max_page_size=self.MAX_PAGE_SIZE,
                max_pages=self.max_pages,
                max_items=max_items,
            ),
        )
        return items[:limit] if limit is not None else items

    def list_components(
        self,
        *,
        app: str,
        limit: int | None = None,
        component_type: str | None = "action",
        registry: str = "public",
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch Pipedream public components/actions with bounded pagination."""

        access_token, token_error = self._access_token()
        if token_error:
            raise ValueError(token_error["message"])

        import requests

        max_items = min(limit, self.max_items) if limit is not None else self.max_items
        page_size = min(limit or self.MAX_PAGE_SIZE, self.MAX_PAGE_SIZE)

        def fetch_page(cursor: str | None, page_size: int) -> CursorPage:
            params: dict[str, Any] = {
                "limit": page_size,
                "app": app,
                "registry": registry,
            }
            if cursor:
                params["after"] = cursor
            if component_type:
                params["component_type"] = component_type
            if query:
                params["q"] = query
            response = requests.get(
                f"{self.base_url}/components",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "X-PD-Environment": self.environment,
                },
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return _pipedream_cursor_page(data)

        items = collect_cursor_pages(
            fetch_page,
            limits=PaginationLimits(
                page_size=page_size,
                max_page_size=self.MAX_PAGE_SIZE,
                max_pages=self.max_pages,
                max_items=max_items,
            ),
        )
        return items[:limit] if limit is not None else items

    def _access_token(self) -> tuple[str | None, dict[str, str] | None]:
        """Resolve a server-side OAuth token for Pipedream API requests."""

        if self.client_id and self.client_secret:
            import requests

            payload = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
            if self.oauth_scope:
                payload["scope"] = self.oauth_scope
            try:
                response = requests.post(
                    self.oauth_token_url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                return None, {
                    "code": "provider_auth_failed",
                    "message": f"Failed to mint Pipedream access token: {exc}",
                }
            if isinstance(data, dict) and data.get("access_token"):
                return str(data["access_token"]), None
            return None, {
                "code": "provider_auth_failed",
                "message": "Pipedream token response did not include access_token.",
            }

        if self.access_token:
            return self.access_token, None

        return None, {
            "code": "provider_not_configured",
            "message": (
                "PIPEDREAM_CLIENT_ID and PIPEDREAM_CLIENT_SECRET are required "
                "for live Pipedream execution. PIPEDREAM_ACCESS_TOKEN is still "
                "supported as a short-lived fallback."
            ),
        }

    def execute(self, request: ProviderExecutionRequest) -> ProviderExecutionResult:
        if not self.project_id:
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_endpoint_not_configured",
                    "message": "PIPEDREAM_PROJECT_ID is required for live Pipedream execution.",
                },
            )

        import requests

        access_token, token_error = self._access_token()
        if token_error:
            return ProviderExecutionResult(status="error", error=token_error)

        auth_prop_name = self.auth_prop_name or request.canonical_app_slug
        configured_props = {
            auth_prop_name: {"authProvisionId": request.provider_connection_id},
            **request.arguments,
        }
        payload = {
            "external_user_id": request.user_id or request.connection_id,
            "id": request.provider_tool_id,
            "configured_props": configured_props,
        }
        try:
            response = requests.post(
                f"{self.base_url}/{self.project_id}/actions/run",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "X-PD-Environment": self.environment,
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_request_failed",
                    "message": str(exc),
                },
            )
        if not isinstance(data, dict):
            data = {"data": data}
        return ProviderExecutionResult(status="ok", result=data)

    def health_check(
        self,
        request: ProviderExecutionRequest,
    ) -> ProviderExecutionResult:
        if request.provider_connection_id:
            return ProviderExecutionResult(
                status="ok",
                result={"authProvisionId": request.provider_connection_id},
            )
        return ProviderExecutionResult(
            status="error",
            error={
                "code": "provider_connection_missing",
                "message": "Pipedream authProvisionId is required for health checks.",
            },
        )

    def create_connect_link_url(
        self,
        *,
        external_user_id: str,
        redirect_url: str | None = None,
        allowed_origins: list[str] | None = None,
    ) -> tuple[str | None, dict[str, str] | None]:
        """Create a Pipedream Connect session URL for an end user."""

        if not self.project_id:
            return None, {
                "code": "provider_endpoint_not_configured",
                "message": "PIPEDREAM_PROJECT_ID is required for Pipedream Connect sessions.",
            }

        access_token, token_error = self._access_token()
        if token_error:
            return None, token_error

        import requests

        payload: dict[str, Any] = {"external_user_id": external_user_id}
        if redirect_url:
            payload["success_redirect_uri"] = redirect_url
            payload["error_redirect_uri"] = redirect_url
        if allowed_origins:
            payload["allowed_origins"] = allowed_origins

        try:
            response = requests.post(
                f"{self.base_url}/{self.project_id}/tokens",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "X-PD-Environment": self.environment,
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            return None, {
                "code": "provider_connect_token_failed",
                "message": f"Failed to create Pipedream Connect token: {exc}",
            }
        if isinstance(data, dict) and data.get("connect_link_url"):
            return str(data["connect_link_url"]), None
        return None, {
            "code": "provider_connect_token_failed",
            "message": "Pipedream Connect token response did not include connect_link_url.",
        }

    def create_connect_url(
        self,
        *,
        external_user_id: str,
        app: dict[str, Any] | None = None,
        connection_id: str | None = None,
        redirect_url: str | None = None,
        allowed_origins: list[str] | None = None,
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        connect_url, error = self.create_connect_link_url(
            external_user_id=external_user_id,
            redirect_url=redirect_url,
            allowed_origins=allowed_origins,
        )
        return connect_url, None, error


# ---------------------------------------------------------------------------
# Pipedream wire-format normalizers
#
# These map Pipedream app/component payloads onto Orchestra's canonical,
# provider-neutral catalog shapes. They are private to the Pipedream adapter; the
# generic sync layer never imports them.
# ---------------------------------------------------------------------------


def _pipedream_behavior_hints(component: dict[str, Any]) -> list[str]:
    annotations = component.get("annotations")
    return normalized_behavior_hints(
        tags=set(),
        annotations=annotations if isinstance(annotations, dict) else None,
    )


def _pipedream_action_class(component: dict[str, Any]) -> str:
    return action_class_from_behavior_hints(_pipedream_behavior_hints(component))


def _pipedream_app_slug(app: dict[str, Any]) -> str:
    value = (
        app.get("name_slug")
        or app.get("slug")
        or app.get("id")
        or app.get("name")
        or ""
    )
    return slugify(str(value))


def _pipedream_tool_name(provider_tool_id: str, canonical_app_slug: str) -> str:
    normalized_tool = provider_tool_id.strip()
    for prefix in (
        f"{canonical_app_slug}-",
        f"{canonical_app_slug}_",
        f"{canonical_app_slug}.",
    ):
        if normalized_tool.startswith(prefix):
            return slugify(normalized_tool[len(prefix) :])
    return slugify(normalized_tool)


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


def _pipedream_cursor_page(data: dict[str, Any]) -> CursorPage:
    page_info = data.get("page_info") if isinstance(data.get("page_info"), dict) else {}
    items = (
        data.get("data")
        or data.get("items")
        or data.get("apps")
        or data.get("components")
        or []
    )
    next_cursor = data.get("next_cursor") or data.get("nextCursor")
    if not next_cursor:
        count = int_or_none(page_info.get("count"))
        total_count = int_or_none(
            page_info.get("total_count") or page_info.get("totalCount"),
        )
        if total_count is None or count is None or count > 0:
            next_cursor = page_info.get("end_cursor") or page_info.get("endCursor")
    return CursorPage(
        items=(
            [item for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else []
        ),
        next_cursor=str(next_cursor) if next_cursor else None,
        total_items=int_or_none(
            page_info.get("total_count") or page_info.get("totalCount"),
        ),
    )
