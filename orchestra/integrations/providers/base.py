"""Base types for integration provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, TypedDict


class ProviderAppEntry(TypedDict, total=False):
    """Canonical, provider-neutral app catalog entry.

    This is the superset shape every provider adapter emits and that the generic
    catalog row builders consume. The union spans the capabilities of all
    backends; an adapter simply omits fields its backend does not expose (e.g. an
    API-key-only app has no ``available_scopes``; an OAuth-only app has no
    ``api_key_schema``). It stays a plain ``dict`` at runtime so downstream
    builders can keep reading it with ``.get()``.
    """

    backend_id: str
    provider_app_id: str
    canonical_app_slug: str
    display_name: str
    description: str | None
    category: str | None
    categories: list[str]
    tags: list[str]
    icon_url: str | None
    auth_modes: list[str]
    available_scopes: list[dict[str, Any]]
    recommended_scopes: list[dict[str, Any]]
    api_key_schema: dict[str, Any] | None
    tool_count: int
    source_type: str
    supported: bool
    # Whether the provider supplies managed OAuth credentials for this app.
    managed_auth: bool
    # OAuth-capable app with no managed credentials -> needs an operator-supplied
    # ("bring your own") OAuth app before it can be connected (e.g. TikTok).
    requires_custom_oauth: bool
    raw_provider_metadata: dict[str, Any]


class ProviderToolEntry(TypedDict, total=False):
    """Canonical, provider-neutral tool/action catalog entry (superset shape)."""

    backend_id: str
    provider_app_id: str
    canonical_app_slug: str
    provider_tool_id: str
    name: str
    display_name: str
    description: str
    required_scopes: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    action_class: str
    behavior_hints: list[str]
    confirmation_required: bool
    category: str | None
    tags: list[str]
    raw_provider_metadata: dict[str, Any]


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

    #: Per-call accounting populated by ``list_app_entries`` so the generic sync
    #: layer can build diagnostics without any provider-specific knowledge.
    last_requested_app_slugs: list[str]
    last_skipped_apps: list[dict[str, Any]]
    last_auth_configs_created: int
    last_auth_configs_reused: int

    def __init__(self) -> None:
        self._reset_catalog_accounting()

    def _reset_catalog_accounting(self) -> None:
        self.last_requested_app_slugs = []
        self.last_skipped_apps = []
        self.last_auth_configs_created = 0
        self.last_auth_configs_reused = 0

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

    def list_app_entries(
        self,
        *,
        app_slugs: list[str] | None = None,
        include_all: bool = False,
        create_auth_configs: bool = False,
        include_detail: bool = True,
    ) -> list[ProviderAppEntry]:
        """Return canonical app entries for the selected (or all) backend apps.

        The adapter owns its own slug semantics, selection, optional auth-config
        creation, and any detail fetches required to populate the superset
        fields. ``include_detail`` is a provider-neutral hint: when false the
        adapter may skip expensive per-app detail enrichment (scopes, credential
        schemas) and return only the lightweight listing needed to resolve slugs
        — useful for the tool phase, which never materializes app rows.
        Selection/auth accounting is recorded on the ``last_*`` attributes so the
        generic sync layer never needs provider knowledge. Backends without
        catalog support return an empty list.
        """

        self._reset_catalog_accounting()
        return []

    def list_tool_entries(
        self,
        *,
        app_slug: str,
        provider_app_id: str | None = None,
        limit: int | None = None,
    ) -> list[ProviderToolEntry]:
        """Return canonical tool entries for one app. Backends may override."""

        return []

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
