"""Provider-trigger Prometheus metrics refresh tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prometheus_client import REGISTRY
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import ProviderTriggerWorkerHeartbeat
from orchestra.observability.provider_trigger_metrics_refresh import (
    refresh_provider_trigger_metrics,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.workers.provider_trigger_worker import WORKER_KEY


def _metric_value(name: str) -> float:
    for metric in REGISTRY.collect():
        if metric.name != name:
            continue
        for sample in metric.samples:
            if sample.name.endswith("_created"):
                continue
            return float(sample.value)
    raise AssertionError(f"metric {name} not found")


def test_provider_trigger_metrics_refresh_publishes_worker_and_backlog_gauges(
    dbsession: Session,
) -> None:
    dao = ProviderTriggerDAO(dbsession)
    binding = dao.create_binding(
        binding_id="binding-metrics-test",
        project_id=1,
        tasks_context_id=1,
        source_task_log_id=9001,
        task_id=9001,
        assistant_id=7,
        task_revision=1,
        trigger=ProviderEventTrigger(
            state="enabled",
            connection_id="conn-metrics",
            backend_id="composio",
            canonical_app_slug="github",
            provider_trigger_slug="GITHUB_ISSUE_CREATED_TRIGGER",
            trigger_config={},
        ),
        execution_mode="live",
        entrypoint=None,
    )
    binding.reconcile_next_retry_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    heartbeat = ProviderTriggerWorkerHeartbeat(
        worker_key=WORKER_KEY,
        lease_owner="metrics-test",
        last_heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        metadata_json={},
    )
    dbsession.add(heartbeat)
    dbsession.flush()

    refresh_provider_trigger_metrics()

    assert (
        _metric_value("orchestra_provider_trigger_worker_heartbeat_age_seconds") >= 25
    )
    assert (
        _metric_value("orchestra_provider_trigger_reconcile_backlog_age_seconds") >= 110
    )
