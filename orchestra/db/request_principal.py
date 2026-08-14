"""Request-scoped tenant principal for integration authorization.

The integration DAO is instantiated deep inside operations/services with only a
``Session``; it has no view of the authenticated caller. Rather than thread a
principal through every signature, the web layer stamps the caller's identity
into this context variable (via an async-generator dependency, the only
dependency kind whose ``ContextVar`` writes reliably reach sync path operations)
and the DAO reads it back to scope every connection/audit lookup.

Fail closed: a guarded lookup with no principal in context raises. Non-request
callers (webhook ingress, cron reconciliation, bootstrap) that legitimately act
across tenants must opt in explicitly with :func:`system_principal`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class RequestPrincipal:
    """The tenant identity a request/flow is authorized to act as.

    ``is_system`` marks a trusted caller (Orchestra admin key or an internal
    flow) that bypasses tenant scoping. A non-system principal must carry at
    least one of ``user_id``/``organization_id`` to resolve to any rows.
    """

    user_id: Optional[str] = None
    organization_id: Optional[int] = None
    is_system: bool = False


_principal_ctx: ContextVar[Optional[RequestPrincipal]] = ContextVar(
    "integration_request_principal",
    default=None,
)


def set_request_principal(principal: RequestPrincipal) -> Token:
    return _principal_ctx.set(principal)


def get_request_principal() -> Optional[RequestPrincipal]:
    return _principal_ctx.get()


def reset_request_principal(token: Token) -> None:
    _principal_ctx.reset(token)


@contextmanager
def use_request_principal(principal: RequestPrincipal) -> Iterator[None]:
    token = _principal_ctx.set(principal)
    try:
        yield
    finally:
        _principal_ctx.reset(token)


@contextmanager
def system_principal() -> Iterator[None]:
    """Run a trusted, non-request internal flow as a tenant-scope bypass."""

    with use_request_principal(RequestPrincipal(is_system=True)):
        yield
