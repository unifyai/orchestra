"""Registry for integration provider adapter classes."""

from __future__ import annotations

import os
from typing import Type

from orchestra.integrations.providers.base import BaseIntegrationProviderAdapter
from orchestra.integrations.providers.composio import ComposioProviderAdapter
from orchestra.integrations.providers.local_echo import LocalEchoProviderAdapter
from orchestra.integrations.providers.pipedream import PipedreamProviderAdapter

PROVIDER_ADAPTERS: dict[str, Type[BaseIntegrationProviderAdapter]] = {
    "composio": ComposioProviderAdapter,
    "pipedream": PipedreamProviderAdapter,
}


def get_provider_adapter(
    backend_id: str,
    *,
    backend_config: dict | None = None,
    backend_status: str = "enabled",
    require_live: bool = False,
) -> BaseIntegrationProviderAdapter:
    """Return the adapter for a backend row.

    Backend rows are the deployment switchboard: ``status`` controls whether a
    provider participates, while deployment environment variables supply
    provider credentials/endpoints. ``config_json`` is reserved for operational
    knobs like timeout/pagination limits, not secret names or provider URLs.
    Adding a provider should mean registering a subclass here, not branching
    through API route logic.
    """

    config = backend_config or {}
    execution_mode = _execution_mode(
        backend_id,
        backend_status=backend_status,
        config=config,
        require_live=require_live,
    )
    if execution_mode != "live":
        return LocalEchoProviderAdapter()
    adapter_cls = PROVIDER_ADAPTERS.get(backend_id)
    if adapter_cls is ComposioProviderAdapter:
        return ComposioProviderAdapter(
            timeout_seconds=int(config.get("timeout_seconds", 30)),
            max_pages=int(config.get("max_pages", 500)),
            max_items=int(config.get("max_items", 100_000)),
        )
    if adapter_cls is PipedreamProviderAdapter:
        return PipedreamProviderAdapter(
            timeout_seconds=int(config.get("timeout_seconds", 30)),
            max_pages=int(config.get("max_pages", 1000)),
            max_items=int(config.get("max_items", 100_000)),
        )
    return LocalEchoProviderAdapter()


def _execution_mode(
    backend_id: str,
    *,
    backend_status: str,
    config: dict,
    require_live: bool,
) -> str:
    if require_live:
        return "live"
    explicit_mode = config.get("execution_mode")
    if explicit_mode:
        return str(explicit_mode)
    if backend_status != "enabled":
        return "local_echo"
    if backend_id == "composio" and os.getenv("COMPOSIO_API_KEY"):
        return "live"
    if backend_id == "pipedream" and (
        os.getenv("PIPEDREAM_ACCESS_TOKEN")
        or (os.getenv("PIPEDREAM_CLIENT_ID") and os.getenv("PIPEDREAM_CLIENT_SECRET"))
    ):
        return "live"
    return "local_echo"
