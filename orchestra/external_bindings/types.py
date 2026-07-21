"""Types for external field binding connectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConnectorAuth:
    """Resolved auth material for a connector call.

    Secrets are resolved server-side from ``auth_secret_ref`` (or env) and never
    returned on ``get_fields``.
    """

    secret_value: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BindingItem:
    """One row's inputs for a single external field hydrate."""

    log_event_id: int
    inputs: dict[str, Any]
    group_key: tuple[Any, ...] = ()


@dataclass(frozen=True)
class BindingResult:
    """Outcome of hydrating one ``BindingItem``."""

    log_event_id: int
    value: Any = None
    error: Optional[str] = None
    external_token: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class WriteResult:
    """Outcome of delivering one external write intent."""

    ok: bool
    response: Any = None
    error: Optional[str] = None
    external_token: Optional[str] = None


@runtime_checkable
class ExternalConnector(Protocol):
    """Batch-capable external data source for bound columns."""

    id: str

    def batch_fetch(
        self,
        *,
        binding: dict[str, Any],
        items: list[BindingItem],
        auth: ConnectorAuth,
    ) -> list[BindingResult]:
        """Fetch values for many items in one planner invocation.

        Implementations should coalesce HTTP where the remote API allows
        batching; otherwise apply bounded concurrency. Never require the
        planner to call this once per cell.
        """
        ...

    def execute_write(
        self,
        *,
        binding: dict[str, Any],
        payload: dict[str, Any],
        idempotency_key: str,
        auth: ConnectorAuth,
    ) -> WriteResult:
        """Deliver a through-write against the external system."""
        ...
