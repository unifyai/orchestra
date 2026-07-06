"""Composio integration provider adapter."""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    provider_tags,
    slugify,
)
from orchestra.integrations.providers.utils.pagination import (
    CursorPage,
    PaginationLimits,
    collect_cursor_pages,
    int_or_none,
)

logger = logging.getLogger(__name__)


class ComposioProviderAdapter(BaseIntegrationProviderAdapter):
    """HTTP adapter for live Composio catalog sync, auth links, and execution."""

    backend_id = "composio"
    MAX_PAGE_SIZE = 1000

    def __init__(
        self,
        *,
        api_key: str | None = None,
        execute_url: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 30,
        max_pages: int = 500,
        max_items: int = 100_000,
        detail_fetch_concurrency: int = 8,
    ) -> None:
        super().__init__()
        self.api_key = api_key or os.getenv("COMPOSIO_API_KEY")
        self.base_url = (
            base_url
            or os.getenv("COMPOSIO_BASE_URL")
            or "https://backend.composio.dev/api/v3.1"
        ).rstrip("/")
        self.execute_url = (
            execute_url
            or os.getenv("COMPOSIO_ACTION_EXECUTE_URL")
            or f"{self.base_url}/tools/execute"
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.max_items = max_items
        self.detail_fetch_concurrency = max(1, int(detail_fetch_concurrency))
        self.last_auth_config_was_created = False
        self._toolkit_by_provider_slug: dict[str, dict[str, Any]] = {}
        self._toolkit_alias: dict[str, str] = {}

    def _api_key_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key or "",
            "Content-Type": "application/json",
        }

    def iter_apps(
        self,
        *,
        limit: int | None = None,
        search: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        return iter(
            self.list_toolkits(page_size=limit or self.MAX_PAGE_SIZE, search=search),
        )

    def iter_tools(
        self,
        *,
        app_id: str,
        limit: int | None = None,
    ) -> Iterable[dict[str, Any]]:
        return iter(self.list_tools(toolkit_slug=app_id, limit=limit))

    def list_toolkits(
        self,
        *,
        page_size: int = MAX_PAGE_SIZE,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all available Composio toolkits with bounded cursor pagination."""

        if not self.api_key:
            raise ValueError("COMPOSIO_API_KEY is required for Composio catalog sync.")

        import requests

        def fetch_page(cursor: str | None, page_size: int) -> CursorPage:
            params: dict[str, Any] = {"limit": page_size}
            if search:
                params["search"] = search
            if cursor:
                params["cursor"] = cursor
            response = requests.get(
                f"{self.base_url}/toolkits",
                headers=self._api_key_headers(),
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return CursorPage(
                items=_items_from_response(data, "toolkits"),
                next_cursor=data.get("next_cursor") or data.get("nextCursor"),
                total_items=int_or_none(
                    data.get("total_items") or data.get("totalItems"),
                ),
                total_pages=int_or_none(
                    data.get("total_pages") or data.get("totalPages"),
                ),
                current_page=int_or_none(
                    data.get("current_page") or data.get("currentPage"),
                ),
            )

        return collect_cursor_pages(
            fetch_page,
            limits=PaginationLimits(
                page_size=page_size,
                max_page_size=self.MAX_PAGE_SIZE,
                max_pages=self.max_pages,
                max_items=self.max_items,
            ),
        )

    def list_tools(
        self,
        *,
        toolkit_slug: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch available Composio tools for one toolkit with bounded pagination."""

        if not self.api_key:
            raise ValueError("COMPOSIO_API_KEY is required for Composio tool sync.")

        import requests

        max_items = min(limit, self.max_items) if limit is not None else self.max_items
        page_size = min(limit or self.MAX_PAGE_SIZE, self.MAX_PAGE_SIZE)

        def fetch_page(cursor: str | None, page_size: int) -> CursorPage:
            params: dict[str, Any] = {"toolkit_slug": toolkit_slug, "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            response = requests.get(
                f"{self.base_url}/tools",
                headers=self._api_key_headers(),
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return CursorPage(
                items=_items_from_response(data, "tools"),
                next_cursor=data.get("next_cursor") or data.get("nextCursor"),
                total_items=int_or_none(
                    data.get("total_items") or data.get("totalItems"),
                ),
                total_pages=int_or_none(
                    data.get("total_pages") or data.get("totalPages"),
                ),
                current_page=int_or_none(
                    data.get("current_page") or data.get("currentPage"),
                ),
            )

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

    def get_toolkit(self, slug: str) -> dict[str, Any]:
        """Fetch one toolkit's full detail document (auth schemes, scopes, meta)."""

        if not self.api_key:
            raise ValueError("COMPOSIO_API_KEY is required for Composio catalog sync.")

        import requests

        response = requests.get(
            f"{self.base_url}/toolkits/{slug.lower()}",
            headers=self._api_key_headers(),
            params={},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def list_app_entries(
        self,
        *,
        app_slugs: list[str] | None = None,
        include_all: bool = False,
        create_auth_configs: bool = False,
        include_detail: bool = True,
    ) -> list[ProviderAppEntry]:
        """Select Composio toolkits and emit canonical app entries.

        Owns toolkit selection, optional managed-auth config creation, and the
        per-toolkit detail fetch (bounded concurrency) used to populate OAuth
        scopes, the API-key credential schema, and tool counts. The detail fetch
        is skipped when ``include_detail`` is false (e.g. the tool phase only
        needs the toolkit list to resolve slugs).
        """

        self._reset_catalog_accounting()
        toolkits = self.list_toolkits()
        self._toolkit_by_provider_slug = {
            str(
                toolkit.get("slug")
                or toolkit.get("toolkit_slug")
                or toolkit.get("id")
                or "",
            ).upper(): toolkit
            for toolkit in toolkits
            if toolkit.get("slug") or toolkit.get("toolkit_slug") or toolkit.get("id")
        }
        # Requests can arrive keyed by the provider's real toolkit slug or by the
        # derived canonical app slug. The canonical slug recovers word boundaries
        # from the display name, so it diverges from the provider slug for most
        # multi-word apps (``LISTENNOTES`` -> ``listen_notes``). Resolve either
        # spelling back to the real toolkit slug so tool fetches never silently
        # drop renamed apps.
        self._toolkit_alias = {}
        for real_slug, toolkit in self._toolkit_by_provider_slug.items():
            self._toolkit_alias.setdefault(real_slug, real_slug)
            canonical = _composio_canonical_app_slug(
                real_slug,
                toolkit.get("name"),
            ).upper()
            self._toolkit_alias.setdefault(canonical, real_slug)
        requested = [slug.strip().upper() for slug in (app_slugs or []) if slug.strip()]
        self.last_requested_app_slugs = requested
        if include_all or not requested:
            selected = sorted(self._toolkit_by_provider_slug)
        else:
            selected = list(
                dict.fromkeys(
                    self._toolkit_alias[slug]
                    for slug in requested
                    if slug in self._toolkit_alias
                ),
            )
        self.last_skipped_apps = [
            {"slug": slug, "reason": "not_found"}
            for slug in requested
            if slug not in self._toolkit_alias
        ]
        should_create_auth_configs = create_auth_configs and bool(requested)

        details: dict[str, dict[str, Any]] = {}
        if include_detail and selected:
            details = self._fetch_toolkit_details(selected)

        entries: list[ProviderAppEntry] = []
        for toolkit_slug in selected:
            toolkit = self._toolkit_by_provider_slug[toolkit_slug]
            canonical_app_slug = _composio_canonical_app_slug(
                toolkit_slug,
                toolkit.get("name"),
            )
            detail = details.get(toolkit_slug) or {}
            auth_modes = _composio_auth_modes(toolkit, detail)
            requires_custom_oauth = _composio_requires_custom_oauth(
                toolkit,
                detail,
                auth_modes,
            )
            managed_auth = not requires_custom_oauth
            auth_config_id = None
            if should_create_auth_configs and "oauth" in auth_modes:
                try:
                    auth_config_id = self.get_or_create_auth_config(toolkit_slug)
                except Exception as exc:
                    self.last_skipped_apps.append(
                        {
                            "slug": toolkit_slug,
                            "reason": "auth_config_failed",
                            "message": str(exc)[:300],
                        },
                    )
                    continue
                if self.last_auth_config_was_created:
                    self.last_auth_configs_created += 1
                elif auth_config_id:
                    self.last_auth_configs_reused += 1
            raw_provider_metadata: dict[str, Any] = {
                "source": "composio_live_sync",
                "toolkit_slug": toolkit_slug,
                "toolkit_version": toolkit.get("version"),
                "managed_auth": managed_auth,
                "requires_custom_oauth": requires_custom_oauth,
                "raw_toolkit": toolkit,
            }
            if detail:
                raw_provider_metadata["raw_toolkit_detail"] = detail
            if auth_config_id:
                raw_provider_metadata["auth_config_id"] = auth_config_id
            category_names = _composio_category_names(toolkit, detail)
            entry: ProviderAppEntry = {
                "backend_id": "composio",
                "provider_app_id": toolkit_slug,
                "canonical_app_slug": canonical_app_slug,
                "display_name": toolkit.get("name")
                or canonical_app_slug.replace("_", " ").title(),
                "description": _composio_description(toolkit, detail),
                "category": category_names[0] if category_names else None,
                "categories": category_names,
                "tags": _composio_tag_names(toolkit, detail),
                "icon_url": _composio_icon_url(toolkit),
                "auth_modes": auth_modes,
                "available_scopes": _composio_oauth_scopes(detail),
                "recommended_scopes": _composio_oauth_scopes(detail),
                "api_key_schema": _composio_api_key_schema(detail),
                "tool_count": _composio_tool_count(detail),
                "managed_auth": managed_auth,
                "requires_custom_oauth": requires_custom_oauth,
                "raw_provider_metadata": raw_provider_metadata,
            }
            entries.append(entry)
        return entries

    def list_tool_entries(
        self,
        *,
        app_slug: str,
        provider_app_id: str | None = None,
        limit: int | None = None,
    ) -> list[ProviderToolEntry]:
        """Fetch one toolkit's tools and emit canonical tool entries."""

        provider_slug = str(provider_app_id or app_slug or "").upper()
        if not provider_slug:
            return []
        # Tolerate being called with either the real toolkit slug or the derived
        # canonical app slug; both resolve to the real slug the provider expects.
        provider_slug = self._toolkit_alias.get(provider_slug, provider_slug)
        toolkit = self._toolkit_by_provider_slug.get(provider_slug, {})
        canonical_app_slug = _composio_canonical_app_slug(
            provider_slug,
            toolkit.get("name"),
        )
        category_names = _composio_category_names(toolkit, {})
        category = category_names[0] if category_names else None
        entries: list[ProviderToolEntry] = []
        for tool in self.list_tools(toolkit_slug=provider_slug, limit=limit):
            provider_tool_id = str(tool.get("slug") or tool.get("id") or "")
            if not provider_tool_id:
                continue
            tool_name = _composio_tool_name(provider_tool_id, provider_slug)
            behavior_hints = _composio_behavior_hints(tool)
            action_class = action_class_from_behavior_hints(behavior_hints)
            entries.append(
                {
                    "backend_id": "composio",
                    "provider_app_id": provider_slug,
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
                    "confirmation_required": confirmation_required_for_action_class(
                        action_class,
                    ),
                    "category": category,
                    "tags": [canonical_app_slug, *category_names],
                    "raw_provider_metadata": {
                        "source": "composio_live_sync",
                        "toolkit_slug": provider_slug,
                        "tool_version": tool.get("version"),
                        "raw_tool": tool,
                    },
                },
            )
        return entries

    def _fetch_toolkit_details(
        self,
        provider_slugs: list[str],
    ) -> dict[str, dict[str, Any]]:
        details: dict[str, dict[str, Any]] = {}
        workers = min(self.detail_fetch_concurrency, len(provider_slugs))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_by_slug = {
                executor.submit(self.get_toolkit, slug): slug for slug in provider_slugs
            }
            for future in as_completed(future_by_slug):
                slug = future_by_slug[future]
                try:
                    details[slug] = future.result()
                except Exception as exc:
                    logger.warning(
                        "Composio toolkit detail fetch failed slug=%s error=%s",
                        slug,
                        exc,
                    )
        return details

    @staticmethod
    def _extract_auth_config_id(payload: Any) -> str | None:
        """Pull an auth-config id out of a create/list response envelope."""

        if not isinstance(payload, dict):
            return None
        auth_config = payload.get("auth_config") if isinstance(payload, dict) else None
        auth_config_id = (
            (auth_config or {}).get("id")
            or (auth_config or {}).get("auth_config_id")
            or payload.get("id")
            or payload.get("auth_config_id")
        )
        return str(auth_config_id) if auth_config_id else None

    def default_oauth_callback_url(self) -> str:
        """Composio's OAuth callback that a bring-your-own OAuth app must allow."""

        return f"{self.base_url}/toolkits/auth/callback"

    @staticmethod
    def _provider_toolkit_slug(toolkit_slug: str) -> str:
        """Normalize a toolkit slug to Composio's provider spelling (e.g. ``TIKTOK``)."""

        return str(toolkit_slug or "").strip().upper()

    @staticmethod
    def _format_custom_oauth_scopes(scopes: list[str] | None) -> str | None:
        """Format OAuth scopes the way Composio's REST API expects (CSV string)."""

        cleaned = [scope.strip() for scope in (scopes or []) if scope and scope.strip()]
        return ",".join(cleaned) if cleaned else None

    @staticmethod
    def _http_error_message(exc: Exception) -> str:
        """Extract a user-facing message from a Composio HTTP error response."""

        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        response_text = getattr(response, "text", "") if response is not None else ""
        message = None
        detail_parts: list[str] = []
        if response_text:
            try:
                payload = json.loads(response_text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    message = error.get("message") or error.get("suggested_fix")
                    # The generic "Validation error while processing request"
                    # wrapper hides the useful field-level detail, which lives in
                    # ``errors``; always fold those in so the operator can act.
                    for item in _flatten_validation_errors(error.get("errors")):
                        if item and item not in detail_parts:
                            detail_parts.append(item)
                    fix = error.get("suggested_fix")
                    if fix and fix != message and fix not in detail_parts:
                        detail_parts.append(str(fix))
                elif isinstance(error, str):
                    message = error
                message = message or payload.get("message")
        prefix = (
            f"Request rejected ({status_code})" if status_code else "Request rejected"
        )
        combined = ": ".join(
            part for part in (message, "; ".join(detail_parts)) if part
        )
        if combined:
            return f"{prefix}: {combined}"
        if response_text:
            return f"{prefix}: {response_text[:300]}"
        return prefix

    def get_or_create_auth_config(self, toolkit_slug: str) -> str | None:
        """Reuse or create a Composio-managed auth config for a toolkit."""

        if not self.api_key:
            raise ValueError(
                "COMPOSIO_API_KEY is required for Composio auth config setup.",
            )

        import requests

        self.last_auth_config_was_created = False
        response = requests.get(
            f"{self.base_url}/auth_configs",
            headers=self._api_key_headers(),
            params={
                "toolkit_slug": toolkit_slug,
                "is_composio_managed": True,
                "limit": self.MAX_PAGE_SIZE,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        for item in data.get("items") or data.get("data") or []:
            auth_config_id = self._extract_auth_config_id(item)
            if auth_config_id:
                return auth_config_id

        create_response = requests.post(
            f"{self.base_url}/auth_configs",
            headers=self._api_key_headers(),
            json={
                "toolkit": {"slug": toolkit_slug},
                "auth_config": {
                    "type": "use_composio_managed_auth",
                    "credentials": {},
                    "restrict_to_following_tools": [],
                },
            },
            timeout=self.timeout_seconds,
        )
        create_response.raise_for_status()
        created = create_response.json()
        auth_config_id = self._extract_auth_config_id(created)
        self.last_auth_config_was_created = bool(auth_config_id)
        return auth_config_id

    def create_custom_auth_config(
        self,
        toolkit_slug: str,
        *,
        client_id: str,
        client_secret: str,
        auth_scheme: str = "OAUTH2",
        scopes: list[str] | None = None,
        name: str | None = None,
        oauth_redirect_uri: str | None = None,
    ) -> str:
        """Create a Composio bring-your-own-OAuth (``use_custom_auth``) config.

        Used for toolkits that Composio does not offer managed credentials for
        (e.g. TikTok) or when the operator wants their own branding/scopes. The
        client id/secret are handed to Composio, which vaults them; only the
        returned ``auth_config_id`` should be persisted by Orchestra. Returns the
        new ``auth_config_id`` (raises on transport/provider error).
        """

        if not self.api_key:
            raise ValueError(
                "COMPOSIO_API_KEY is required for Composio auth config setup.",
            )
        if not client_id or not client_secret:
            raise ValueError(
                "client_id and client_secret are required for custom OAuth auth configs.",
            )

        import requests

        self.last_auth_config_was_created = False
        provider_slug = self._provider_toolkit_slug(toolkit_slug)
        credentials: dict[str, Any] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "oauth_redirect_uri": (
                oauth_redirect_uri or self.default_oauth_callback_url()
            ),
        }
        scope_csv = self._format_custom_oauth_scopes(scopes)
        if scope_csv:
            credentials["scopes"] = scope_csv
        # Composio's v3.1 REST endpoint expects the OAuth scheme as camelCase
        # ``authScheme`` (the Python SDK accepts ``auth_scheme`` and serializes
        # it for you; the raw HTTP API does not). Sending snake_case yields a
        # 400 "payload.auth_config.authScheme: Required".
        payload = {
            "toolkit": {"slug": provider_slug},
            "auth_config": {
                "name": name or f"{provider_slug} (custom OAuth)",
                "type": "use_custom_auth",
                "authScheme": auth_scheme,
                "credentials": credentials,
                "restrict_to_following_tools": [],
            },
        }
        try:
            create_response = requests.post(
                f"{self.base_url}/auth_configs",
                headers=self._api_key_headers(),
                json=payload,
                timeout=self.timeout_seconds,
            )
            create_response.raise_for_status()
        except Exception as exc:
            response = getattr(exc, "response", None)
            # Keep the granular provider detail (parsed message, field-level
            # validation errors, raw body, status) in the logs for gcloud
            # debugging only — never leak it through the API response.
            logger.warning(
                "Composio custom OAuth auth config create failed toolkit=%s "
                "auth_scheme=%s status=%s detail=%s body=%s",
                provider_slug,
                auth_scheme,
                getattr(response, "status_code", None),
                self._http_error_message(exc),
                (getattr(response, "text", "") or "")[:1000],
            )
            raise ValueError(
                "Custom OAuth configuration rejected. Check the "
                "client ID, secret, redirect URI, and scopes, then try again.",
            ) from exc
        created = create_response.json()
        auth_config_id = self._extract_auth_config_id(created)
        if not auth_config_id:
            raise ValueError(
                "Composio did not return an auth_config_id for the custom OAuth config.",
            )
        self.last_auth_config_was_created = True
        return auth_config_id

    def delete_auth_config(self, auth_config_id: str) -> None:
        """Best-effort delete of a Composio auth config by id."""

        if not self.api_key or not auth_config_id:
            return

        import requests

        response = requests.delete(
            f"{self.base_url}/auth_configs/{auth_config_id}",
            headers=self._api_key_headers(),
            timeout=self.timeout_seconds,
        )
        # Treat an already-removed config as success.
        if response.status_code not in (200, 202, 204, 404):
            response.raise_for_status()

    def create_auth_link(
        self,
        *,
        user_id: str,
        auth_config_id: str,
        callback_url: str | None = None,
        alias: str | None = None,
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        """Create a Composio auth link and return redirect + connected account."""

        if not self.api_key:
            return (
                None,
                None,
                {
                    "code": "provider_not_configured",
                    "message": "COMPOSIO_API_KEY is required for Composio auth links.",
                },
            )

        import requests

        payload: dict[str, Any] = {
            "user_id": user_id,
            "auth_config_id": auth_config_id,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if alias:
            payload["alias"] = alias
        try:
            response = requests.post(
                f"{self.base_url}/connected_accounts/link",
                headers=self._api_key_headers(),
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            return (
                None,
                None,
                {
                    "code": "provider_auth_link_failed",
                    "message": f"Failed to create Composio auth link: {exc}",
                },
            )
        redirect_url = data.get("redirect_url") or data.get("redirectUrl")
        connected_account_id = (
            data.get("connected_account_id")
            or data.get("connectedAccountId")
            or data.get("id")
        )
        if redirect_url:
            return (
                str(redirect_url),
                str(connected_account_id) if connected_account_id else None,
                None,
            )
        return (
            None,
            None,
            {
                "code": "provider_auth_link_failed",
                "message": "Composio auth link response did not include redirect_url.",
            },
        )

    def create_connect_url(
        self,
        *,
        external_user_id: str,
        app: dict[str, Any] | None = None,
        connection_id: str | None = None,
        redirect_url: str | None = None,
        allowed_origins: list[str] | None = None,
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        auth_config_id = (app or {}).get("auth_config_id")
        if not auth_config_id:
            return (
                None,
                None,
                {
                    "code": "provider_auth_config_missing",
                    "message": "Composio auth_config_id is required for provider connect URLs.",
                },
            )
        return self.create_auth_link(
            user_id=external_user_id,
            auth_config_id=str(auth_config_id),
            callback_url=redirect_url,
            alias=connection_id,
        )

    def stage_file(
        self,
        *,
        content: bytes,
        filename: str,
        mimetype: str,
        toolkit_slug: str,
        tool_slug: str,
    ) -> dict[str, Any]:
        """Upload bytes to Composio storage for a FileUploadable tool argument."""
        if not self.api_key:
            return {
                "status": "error",
                "error": {
                    "code": "provider_not_configured",
                    "message": "COMPOSIO_API_KEY is required for Composio file staging.",
                },
            }
        import hashlib

        import requests

        md5_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
        try:
            response = requests.post(
                f"{self.base_url}/files/upload/request",
                headers=self._api_key_headers(),
                json={
                    "md5": md5_hash,
                    "filename": filename,
                    "mimetype": mimetype,
                    "tool_slug": tool_slug,
                    "toolkit_slug": toolkit_slug,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            meta = response.json()
        except Exception as exc:
            return {
                "status": "error",
                "error": {
                    "code": "provider_file_stage_failed",
                    "message": str(exc),
                },
            }

        key = str(meta.get("key") or "")
        presigned = meta.get("new_presigned_url") or meta.get("newPresignedUrl")
        if presigned:
            try:
                upload = requests.put(
                    str(presigned),
                    data=content,
                    headers={"Content-Type": mimetype},
                    timeout=max(self.timeout_seconds, 120),
                )
                upload.raise_for_status()
            except Exception as exc:
                return {
                    "status": "error",
                    "error": {
                        "code": "provider_file_upload_failed",
                        "message": str(exc),
                    },
                }
        if not key:
            return {
                "status": "error",
                "error": {
                    "code": "provider_file_stage_failed",
                    "message": "Composio file staging returned no storage key.",
                },
            }
        return {
            "status": "ok",
            "file": {
                "name": filename,
                "mimetype": mimetype,
                "s3key": key,
            },
        }

    def execute(self, request: ProviderExecutionRequest) -> ProviderExecutionResult:
        if not self.api_key:
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_not_configured",
                    "message": "COMPOSIO_API_KEY is required for live Composio execution.",
                },
            )
        if not self.execute_url:
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_endpoint_not_configured",
                    "message": "COMPOSIO_ACTION_EXECUTE_URL is required for live Composio execution.",
                },
            )

        import requests

        payload: dict[str, Any] = {
            "arguments": request.arguments,
            "user_id": request.user_id or request.connection_id,
            "connected_account_id": request.provider_connection_id,
        }
        if request.version:
            payload["version"] = request.version
        try:
            response = requests.post(
                f"{self.execute_url}/{request.provider_tool_id}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            response = getattr(exc, "response", None)
            provider_status_code = getattr(response, "status_code", None)
            provider_response_body = ""
            if response is not None:
                provider_response_body = str(getattr(response, "text", "") or "")[:1000]
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_request_failed",
                    "message": str(exc),
                    "provider_status_code": provider_status_code,
                    "provider_response_body": provider_response_body,
                    "provider_request": {
                        "provider_tool_id": request.provider_tool_id,
                        "payload_keys": sorted(payload.keys()),
                        "argument_keys": sorted(request.arguments.keys()),
                        "user_id_present": bool(payload.get("user_id")),
                        "connected_account_id_present": bool(
                            payload.get("connected_account_id"),
                        ),
                    },
                },
            )
        if not isinstance(data, dict):
            data = {"data": data}
        return ProviderExecutionResult(status="ok", result=data)

    def health_check(
        self,
        request: ProviderExecutionRequest,
    ) -> ProviderExecutionResult:
        if request.provider_connection_id and self.api_key:
            import requests

            try:
                response = requests.get(
                    f"{self.base_url}/connected_accounts/{request.provider_connection_id}",
                    headers=self._api_key_headers(),
                    params={},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                return ProviderExecutionResult(
                    status="error",
                    error={
                        "code": "provider_health_check_failed",
                        "message": str(exc),
                    },
                )
            provider_status = str(data.get("status") or "").upper()
            if provider_status in {"ACTIVE", "CONNECTED"}:
                return ProviderExecutionResult(
                    status="ok",
                    result={
                        "connected_account_id": request.provider_connection_id,
                        "provider_status": provider_status,
                    },
                )
            return ProviderExecutionResult(
                status="error",
                error={
                    "code": "provider_connection_not_active",
                    "message": f"Composio connected account status is {provider_status or 'unknown'}.",
                },
            )
        if request.provider_connection_id:
            return ProviderExecutionResult(
                status="ok",
                result={"connected_account_id": request.provider_connection_id},
            )
        return ProviderExecutionResult(
            status="error",
            error={
                "code": "provider_connection_missing",
                "message": "Composio connected_account_id is required for health checks.",
            },
        )


def _items_from_response(
    data: dict[str, Any],
    fallback_key: str,
) -> list[dict[str, Any]]:
    batch = data.get("items") or data.get("data") or data.get(fallback_key) or []
    return (
        [item for item in batch if isinstance(item, dict)]
        if isinstance(batch, list)
        else []
    )


# ---------------------------------------------------------------------------
# Composio wire-format normalizers
#
# Everything below maps Composio's toolkit/tool payloads onto Orchestra's
# canonical, provider-neutral catalog shapes. These functions are private to the
# Composio adapter; the generic sync layer never imports them.
# ---------------------------------------------------------------------------


def _toolkit_meta(toolkit: dict[str, Any]) -> dict[str, Any]:
    meta = toolkit.get("meta")
    return meta if isinstance(meta, dict) else {}


def _composio_canonical_app_slug(
    provider_app_id: str,
    display_name: str | None = None,
) -> str:
    """Derive the canonical app slug, recovering word boundaries from the name.

    Composio toolkit slugs concatenate multi-word names without separators
    (``GOOGLEDRIVE``), so slugifying the slug alone cannot recover the boundary.
    The human display name carries it (``"Google Drive"`` -> ``google_drive``),
    which generalizes to every app with no per-app curation. Single-word brands
    are unaffected (``"Ably"`` -> ``ably``). Falls back to the slug when no name
    is available.
    """

    if display_name and display_name.strip():
        slug = slugify(display_name)
        if slug:
            return slug
    return slugify(provider_app_id)


def _composio_tool_name(provider_tool_id: str, provider_app_id: str) -> str:
    normalized_tool = provider_tool_id.strip().upper()
    normalized_app = provider_app_id.strip().upper()
    for prefix in (f"{normalized_app}_", f"{normalized_app}."):
        if normalized_tool.startswith(prefix):
            return slugify(normalized_tool[len(prefix) :])
    return slugify(normalized_tool)


def _composio_auth_modes(
    toolkit: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> list[str]:
    schemes = (
        toolkit.get("auth_schemes")
        or toolkit.get("authSchemes")
        or toolkit.get("auth")
        or []
    )
    if isinstance(schemes, str):
        schemes = [schemes]
    raw_modes = [str(scheme) for scheme in schemes]
    for entry in _auth_config_details(detail):
        mode = entry.get("mode")
        if mode:
            raw_modes.append(str(mode))
    modes: list[str] = []
    for raw in raw_modes:
        normalized = raw.upper()
        if "OAUTH" in normalized:
            mode = "oauth"
        elif "API" in normalized or "TOKEN" in normalized or "KEY" in normalized:
            mode = "api_key"
        elif "NO_AUTH" in normalized:
            mode = "custom"
        else:
            continue
        if mode not in modes:
            modes.append(mode)
    return modes or ["oauth"]


def _flatten_validation_errors(errors: Any) -> list[str]:
    """Render Composio/Zod-style validation error entries as readable strings.

    Composio wraps field errors under ``error.errors`` as a list of dicts (often
    Zod issues with ``path`` + ``message``). Flatten them to ``path: message`` so
    the operator sees which field failed instead of a generic wrapper.
    """

    if not isinstance(errors, list):
        return []
    rendered: list[str] = []
    for entry in errors:
        if isinstance(entry, str):
            rendered.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") or entry.get("msg")
        path = entry.get("path") or entry.get("loc")
        if isinstance(path, list):
            path_str = ".".join(str(part) for part in path if part not in (None, ""))
        else:
            path_str = str(path) if path else ""
        if message and path_str:
            rendered.append(f"{path_str}: {message}")
        elif message:
            rendered.append(str(message))
        elif path_str:
            rendered.append(path_str)
    return rendered


def _composio_managed_auth_schemes(
    toolkit: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> list[str] | None:
    """Auth schemes Composio provides managed credentials for.

    Returns ``None`` when the payload gives no signal (older responses), so
    callers can distinguish "unknown" from "definitely none".
    """

    for source in (detail, toolkit):
        if not isinstance(source, dict):
            continue
        raw = source.get("composio_managed_auth_schemes") or source.get(
            "composioManagedAuthSchemes",
        )
        if isinstance(raw, list):
            return [str(item).upper() for item in raw]
    return None


def _composio_requires_custom_oauth(
    toolkit: dict[str, Any],
    detail: dict[str, Any] | None,
    auth_modes: list[str],
) -> bool:
    """True when an OAuth toolkit lacks Composio-managed credentials.

    Such toolkits (e.g. TikTok) can only be connected once an operator supplies
    their own OAuth app ("bring your own OAuth"). Conservative by design: when
    Composio gives no managed-auth signal we return ``False`` so managed apps are
    never mislabelled — the connect flow still surfaces a clear error if a
    managed config turns out to be unavailable.
    """

    if "oauth" not in auth_modes:
        return False
    for entry in _auth_config_details(detail):
        if "OAUTH" not in str(entry.get("mode") or "").upper():
            continue
        if "is_composio_managed" in entry:
            return not bool(entry.get("is_composio_managed"))
    managed_schemes = _composio_managed_auth_schemes(toolkit, detail)
    if managed_schemes is None:
        return False
    return not any("OAUTH" in scheme for scheme in managed_schemes)


def _composio_icon_url(toolkit: dict[str, Any]) -> str | None:
    meta = _toolkit_meta(toolkit)
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


def _composio_description(
    toolkit: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> str | None:
    detail_meta = _toolkit_meta(detail or {})
    list_meta = _toolkit_meta(toolkit)
    for value in (
        detail_meta.get("description"),
        list_meta.get("description"),
        toolkit.get("description"),
        (detail or {}).get("description"),
    ):
        if value:
            return str(value)
    return None


def _composio_category_names(
    toolkit: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> list[str]:
    names: list[str] = []
    lowered: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.lower() not in lowered:
            lowered.add(text.lower())
            names.append(text)

    for meta in (_toolkit_meta(detail or {}), _toolkit_meta(toolkit)):
        categories = meta.get("categories")
        if isinstance(categories, list):
            for entry in categories:
                if isinstance(entry, dict):
                    add(entry.get("name") or entry.get("slug") or entry.get("id"))
                else:
                    add(entry)
    add(toolkit.get("category"))
    return names


def _composio_tag_names(
    toolkit: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> list[str]:
    names = list(_composio_category_names(toolkit, detail))
    lowered = {name.lower() for name in names}

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.lower() not in lowered:
            lowered.add(text.lower())
            names.append(text)

    for meta in (_toolkit_meta(detail or {}), _toolkit_meta(toolkit)):
        for key in ("tags", "tag_names", "labels"):
            values = meta.get(key)
            if isinstance(values, list):
                for entry in values:
                    if isinstance(entry, dict):
                        add(
                            entry.get("name")
                            or entry.get("label")
                            or entry.get("slug"),
                        )
                    else:
                        add(entry)
    return names


def _composio_primary_category(
    toolkit: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> str | None:
    names = _composio_category_names(toolkit, detail)
    return names[0] if names else None


def _composio_tool_count(detail: dict[str, Any] | None) -> int:
    meta = _toolkit_meta(detail or {})
    try:
        return int(meta.get("tools_count") or meta.get("toolsCount") or 0)
    except (TypeError, ValueError):
        return 0


def _auth_config_details(detail: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(detail, dict):
        return []
    entries = detail.get("auth_config_details")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _auth_fields(entry: dict[str, Any], section: str) -> list[dict[str, Any]]:
    fields = entry.get("fields")
    if not isinstance(fields, dict):
        return []
    bucket = fields.get(section)
    if not isinstance(bucket, dict):
        return []
    collected: list[dict[str, Any]] = []
    for key in ("required", "optional"):
        items = bucket.get(key)
        if isinstance(items, list):
            collected.extend(item for item in items if isinstance(item, dict))
    return collected


def _composio_oauth_scopes(detail: dict[str, Any] | None) -> list[dict[str, Any]]:
    for entry in _auth_config_details(detail):
        if "OAUTH" not in str(entry.get("mode") or "").upper():
            continue
        for field_def in _auth_fields(entry, "auth_config_creation"):
            if field_def.get("name") != "scopes":
                continue
            default = field_def.get("default")
            if not isinstance(default, str) or not default.strip():
                return []
            return [
                {"name": scope.strip()} for scope in default.split(",") if scope.strip()
            ]
    return []


def _composio_api_key_schema(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    for entry in _auth_config_details(detail):
        if str(entry.get("mode") or "").upper() != "API_KEY":
            continue
        properties: dict[str, Any] = {}
        required: list[str] = []
        for field_def in _auth_fields(entry, "connected_account_initiation"):
            name = field_def.get("name")
            if not name:
                continue
            properties[str(name)] = {
                "type": field_def.get("type") or "string",
                "title": field_def.get("displayName") or field_def.get("name"),
                "description": field_def.get("description") or "",
                "secret": bool(field_def.get("is_secret")),
            }
            if field_def.get("required"):
                required.append(str(name))
        if properties:
            return {
                "type": "object",
                "properties": properties,
                "required": required,
            }
    return None


def _composio_tool_scopes(tool: dict[str, Any]) -> list[str]:
    scopes = tool.get("scopes") or []
    if isinstance(scopes, dict):
        scopes = list(scopes.keys())
    if not isinstance(scopes, list):
        return []
    return [str(scope) for scope in scopes if scope]


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


def _composio_toolkit_slug(tool: dict[str, Any]) -> str | None:
    toolkit = tool.get("toolkit")
    if isinstance(toolkit, dict):
        slug = toolkit.get("slug")
        if slug:
            return str(slug)
    return None


def _composio_behavior_hints(tool: dict[str, Any]) -> list[str]:
    return normalized_behavior_hints(tags=provider_tags(tool.get("tags")))


def _composio_action_class(tool: dict[str, Any]) -> str:
    return action_class_from_behavior_hints(_composio_behavior_hints(tool))
