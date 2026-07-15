"""Deployment topology prerequisites for provider-event triggers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from orchestra.provider_triggers.composio_trigger_adapter import (
    COMPOSIO_WEBHOOK_SECRET_REF,
)
from orchestra.provider_triggers.private_event_storage import (
    provider_event_storage_configured,
)
from orchestra.provider_triggers.runtime_types import ReconcileErrorCode
from orchestra.provider_triggers.signing_secret_refs import resolve_signing_secret_ref
from orchestra.provider_triggers.trigger_adapter_registry import (
    TRIGGER_PROVIDER_ADAPTERS,
)
from orchestra.settings import settings
from orchestra.workers.provider_trigger_worker import WORKER_KEY

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


class TopologyUnavailableReason(StrEnum):
    """Stable reasons provider triggers are unavailable in this deployment."""

    callback_url_unconfigured = "callback_url_unconfigured"
    callback_url_not_https = "callback_url_not_https"
    callback_url_internal = "callback_url_internal"
    event_storage_unconfigured = "event_storage_unconfigured"
    signing_secret_unconfigured = "signing_secret_unconfigured"
    worker_unhealthy = "worker_unhealthy"


_INTERNAL_CALLBACK_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "orchestra",
        "orchestra-trigger-worker",
        "trigger-ingress",
    },
)


@dataclass(frozen=True)
class ProviderTriggerTopologyStatus:
    """Evaluated deployment readiness for provider-event triggers."""

    available: bool
    unavailable_reason: str | None
    callback_base_url: str | None
    event_storage_configured: bool
    signing_configured: bool
    worker_healthy: bool


def _callback_base_url() -> str | None:
    configured = (settings.orchestra_trigger_callback_base_url or "").strip()
    return configured.rstrip("/") if configured else None


def _callback_url_is_public_https(url: str) -> TopologyUnavailableReason | None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return TopologyUnavailableReason.callback_url_not_https
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return TopologyUnavailableReason.callback_url_unconfigured
    if hostname in _INTERNAL_CALLBACK_HOSTS:
        return TopologyUnavailableReason.callback_url_internal
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        return TopologyUnavailableReason.callback_url_internal
    return None


def signing_secrets_configured() -> bool:
    """Return True when registered trigger adapters have signing material."""

    if "composio" in TRIGGER_PROVIDER_ADAPTERS:
        if not resolve_signing_secret_ref(COMPOSIO_WEBHOOK_SECRET_REF):
            return False
    return True


def worker_heartbeat_is_healthy(
    session: Session,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True when the trigger worker heartbeat is fresh."""

    from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO

    heartbeat = ProviderTriggerDAO(session).get_worker_heartbeat(
        worker_key=WORKER_KEY,
    )
    if heartbeat is None or heartbeat.last_heartbeat_at is None:
        return False

    current = now or datetime.now(timezone.utc)
    last_seen = heartbeat.last_heartbeat_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    max_age = timedelta(
        seconds=settings.provider_trigger_worker_heartbeat_max_age_seconds,
    )
    return current - last_seen <= max_age


def evaluate_provider_trigger_topology(
    session: Session | None = None,
    *,
    require_worker: bool = True,
) -> ProviderTriggerTopologyStatus:
    """Evaluate whether provider-event triggers may be configured and activated."""

    callback_base = _callback_base_url()
    storage_configured = provider_event_storage_configured()
    signing_configured = signing_secrets_configured()
    worker_healthy = False
    if session is not None:
        worker_healthy = worker_heartbeat_is_healthy(session)

    unavailable_reason: str | None = None
    if not callback_base:
        unavailable_reason = TopologyUnavailableReason.callback_url_unconfigured.value
    else:
        callback_issue = _callback_url_is_public_https(callback_base)
        if callback_issue is not None:
            unavailable_reason = callback_issue.value
    if unavailable_reason is None and not storage_configured:
        unavailable_reason = TopologyUnavailableReason.event_storage_unconfigured.value
    if unavailable_reason is None and not signing_configured:
        unavailable_reason = TopologyUnavailableReason.signing_secret_unconfigured.value
    if (
        unavailable_reason is None
        and require_worker
        and session is not None
        and not worker_healthy
    ):
        unavailable_reason = TopologyUnavailableReason.worker_unhealthy.value

    available = unavailable_reason is None
    return ProviderTriggerTopologyStatus(
        available=available,
        unavailable_reason=unavailable_reason,
        callback_base_url=callback_base,
        event_storage_configured=storage_configured,
        signing_configured=signing_configured,
        worker_healthy=worker_healthy,
    )


def topology_reason_to_reconcile_error(
    reason: str | None,
) -> ReconcileErrorCode | None:
    """Map topology unavailable reasons to reconciliation error codes."""

    if reason is None:
        return None
    mapping = {
        TopologyUnavailableReason.callback_url_unconfigured.value: (
            ReconcileErrorCode.callback_url_unconfigured
        ),
        TopologyUnavailableReason.callback_url_not_https.value: (
            ReconcileErrorCode.callback_url_unconfigured
        ),
        TopologyUnavailableReason.callback_url_internal.value: (
            ReconcileErrorCode.callback_url_unconfigured
        ),
        TopologyUnavailableReason.event_storage_unconfigured.value: (
            ReconcileErrorCode.event_storage_unconfigured
        ),
        TopologyUnavailableReason.signing_secret_unconfigured.value: (
            ReconcileErrorCode.signing_secret_unconfigured
        ),
        TopologyUnavailableReason.worker_unhealthy.value: (
            ReconcileErrorCode.worker_unhealthy
        ),
    }
    return mapping.get(reason)
