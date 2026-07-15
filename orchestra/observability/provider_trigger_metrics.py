"""Prometheus metrics for provider-event trigger topology and ingress."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

PROVIDER_TRIGGER_INGRESS_REJECTIONS = Counter(
    "orchestra_provider_trigger_ingress_rejections_total",
    "Provider-trigger ingress rejections by backend and reason.",
    ["backend_id", "reason"],
)

PROVIDER_TRIGGER_WORKER_HEARTBEAT_AGE_SECONDS = Gauge(
    "orchestra_provider_trigger_worker_heartbeat_age_seconds",
    "Age in seconds since the provider-trigger worker last heartbeat.",
)

PROVIDER_TRIGGER_RECONCILE_BACKLOG_AGE_SECONDS = Gauge(
    "orchestra_provider_trigger_reconcile_backlog_age_seconds",
    "Age in seconds of the oldest due provider-trigger binding reconcile claim.",
)

PROVIDER_TRIGGER_DISPATCH_BACKLOG_AGE_SECONDS = Gauge(
    "orchestra_provider_trigger_dispatch_backlog_age_seconds",
    "Age in seconds of the oldest pending provider-event dispatch operation.",
)

PROVIDER_TRIGGER_STORAGE_FAILURES = Counter(
    "orchestra_provider_trigger_storage_failures_total",
    "Private provider-event storage failures by operation.",
    ["operation"],
)

PROVIDER_TRIGGER_EVENT_TO_VISIBLE_RUN_SECONDS = Histogram(
    "orchestra_provider_trigger_event_to_visible_run_seconds",
    "Seconds from durable matched acceptance to a visible queued/running run.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)


def record_ingress_rejection(*, backend_id: str, reason: str) -> None:
    """Increment one ingress rejection counter."""

    PROVIDER_TRIGGER_INGRESS_REJECTIONS.labels(
        backend_id=backend_id,
        reason=reason,
    ).inc()


def record_storage_failure(*, operation: str) -> None:
    """Increment one private storage failure counter."""

    PROVIDER_TRIGGER_STORAGE_FAILURES.labels(operation=operation).inc()


def set_worker_heartbeat_age_seconds(age_seconds: float | None) -> None:
    """Publish the current worker heartbeat age."""

    if age_seconds is None:
        PROVIDER_TRIGGER_WORKER_HEARTBEAT_AGE_SECONDS.set(-1)
        return
    PROVIDER_TRIGGER_WORKER_HEARTBEAT_AGE_SECONDS.set(age_seconds)


def set_reconcile_backlog_age_seconds(age_seconds: float | None) -> None:
    """Publish the oldest binding reconcile backlog age."""

    if age_seconds is None:
        PROVIDER_TRIGGER_RECONCILE_BACKLOG_AGE_SECONDS.set(0)
        return
    PROVIDER_TRIGGER_RECONCILE_BACKLOG_AGE_SECONDS.set(age_seconds)


def set_dispatch_backlog_age_seconds(age_seconds: float | None) -> None:
    """Publish the oldest dispatch backlog age."""

    if age_seconds is None:
        PROVIDER_TRIGGER_DISPATCH_BACKLOG_AGE_SECONDS.set(0)
        return
    PROVIDER_TRIGGER_DISPATCH_BACKLOG_AGE_SECONDS.set(age_seconds)


def record_event_to_visible_run_latency(*, latency_seconds: float) -> None:
    """Observe one accept-to-visible-run latency sample."""

    PROVIDER_TRIGGER_EVENT_TO_VISIBLE_RUN_SECONDS.observe(max(0.0, latency_seconds))
