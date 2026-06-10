"""Base types for integration provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class ProviderExecutionRequest:
    """Normalized provider action request passed after Orchestra policy checks."""

    backend_id: str
    tool_id: str
    canonical_app_slug: str
    provider_tool_id: str
    connection_id: str | None
    provider_connection_id: str | None
    action_class: str
    user_id: str | None = None
    version: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderExecutionResult:
    """Provider adapter response normalized before audit persistence."""

    status: str
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None


class BaseIntegrationProviderAdapter(ABC):
    """Inheritance contract for provider-specific backend adapters.

    Adding a backend should mean adding one subclass and registering it in
    ``registry.py``. The API layer should not need provider-specific transport
    logic beyond mapping provider catalog payloads into Orchestra's normalized
    schema.
    """

    backend_id: str = "custom"

    def iter_apps(
        self,
        *,
        limit: int | None = None,
        search: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        """Yield provider app/catalog records. Backends may override if supported."""

        return iter(())

    def iter_tools(
        self,
        *,
        app_id: str,
        limit: int | None = None,
    ) -> Iterable[dict[str, Any]]:
        """Yield provider tool/action records for one app. Backends may override."""

        return iter(())

    def create_connect_url(
        self,
        *,
        external_user_id: str,
        app: dict[str, Any] | None = None,
        connection_id: str | None = None,
        redirect_url: str | None = None,
        allowed_origins: list[str] | None = None,
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        """Create an end-user connection URL.

        Returns ``(url, provider_connection_id, error)``. Providers that do not
        support connection URL creation can return a provider-not-configured
        style error.
        """

        return (
            None,
            None,
            {
                "code": "provider_connect_not_supported",
                "message": f"{self.backend_id} does not support provider connect URL creation.",
            },
        )

    @abstractmethod
    def execute(self, request: ProviderExecutionRequest) -> ProviderExecutionResult:
        """Execute one provider action and return a normalized result."""

    @abstractmethod
    def health_check(
        self,
        request: ProviderExecutionRequest,
    ) -> ProviderExecutionResult:
        """Check whether the provider connection is usable."""
