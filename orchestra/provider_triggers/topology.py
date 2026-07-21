"""Deployment topology prerequisites for provider-event triggers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from orchestra.provider_triggers.backend_ids import (
    COMPOSIO_BACKEND_ID,
    NATIVE_GOOGLE_BACKEND_ID,
    PIPEDREAM_BACKEND_ID,
)
from orchestra.provider_triggers.composio_trigger_adapter import (
    COMPOSIO_WEBHOOK_SECRET_REF,
)
from orchestra.provider_triggers.local_native_google_trigger_adapter import (
    NATIVE_GOOGLE_WEBHOOK_SECRET_REF,
)
from orchestra.provider_triggers.private_event_storage import (
    TriggerKeyWrappingService,
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
    native_google_signing_unconfigured = "native_google_signing_unconfigured"
    native_google_topic_unconfigured = "native_google_topic_unconfigured"


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

    if COMPOSIO_BACKEND_ID in TRIGGER_PROVIDER_ADAPTERS:
        if not resolve_signing_secret_ref(COMPOSIO_WEBHOOK_SECRET_REF):
            return False
    if PIPEDREAM_BACKEND_ID in TRIGGER_PROVIDER_ADAPTERS:
        if not TriggerKeyWrappingService.is_configured():
            return False
    return True


def native_google_signing_configured() -> bool:
    """Return True when the native Google webhook signing secret resolves.

    Adapters signs Meet bridge deliveries and Orchestra verifies them with the
    same ``NATIVE_GOOGLE_WEBHOOK_SECRET``; without it no native Google delivery
    can authenticate.
    """

    return bool(resolve_signing_secret_ref(NATIVE_GOOGLE_WEBHOOK_SECRET_REF))


def native_google_meet_events_topic_configured() -> bool:
    """Return True when the shared Meet Workspace Events topic is configured."""

    return bool((settings.native_google_meet_events_pubsub_topic or "").strip())


def native_google_meet_prerequisites_reason() -> TopologyUnavailableReason | None:
    """Return the first missing native Google Meet prerequisite, or None.

    Native Google Meet transcript triggers need a resolvable webhook signing
    secret and a configured Workspace Events Pub/Sub topic for
    ``notificationEndpoint.pubsubTopic``. Native provision uses this to fail
    closed rather than registering a subscription that can never deliver. It is
    intentionally separate from the global ``signing_secrets_configured`` gate so
    Composio/Pipedream availability does not depend on native Google wiring.
    """

    if NATIVE_GOOGLE_BACKEND_ID not in TRIGGER_PROVIDER_ADAPTERS:
        return None
    if not native_google_signing_configured():
        return TopologyUnavailableReason.native_google_signing_unconfigured
    if not native_google_meet_events_topic_configured():
        return TopologyUnavailableReason.native_google_topic_unconfigured
    return None


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
