"""Composio integration provider adapter."""

from __future__ import annotations

import os
from typing import Any, Iterable

from orchestra.integrations.providers.base import (
    BaseIntegrationProviderAdapter,
    ProviderExecutionRequest,
    ProviderExecutionResult,
)
from orchestra.integrations.providers.pagination import (
    CursorPage,
    PaginationLimits,
    collect_cursor_pages,
    int_or_none,
)


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
    ) -> None:
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
        self.last_auth_config_was_created = False

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
            auth_config = item.get("auth_config") if isinstance(item, dict) else None
            auth_config_id = (
                (auth_config or {}).get("id")
                or (auth_config or {}).get("auth_config_id")
                or (item or {}).get("id")
                or (item or {}).get("auth_config_id")
            )
            if auth_config_id:
                return str(auth_config_id)

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
        auth_config = created.get("auth_config") if isinstance(created, dict) else None
        auth_config_id = (
            (auth_config or {}).get("id")
            or (auth_config or {}).get("auth_config_id")
            or created.get("id")
            or created.get("auth_config_id")
        )
        self.last_auth_config_was_created = bool(auth_config_id)
        return str(auth_config_id) if auth_config_id else None

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
