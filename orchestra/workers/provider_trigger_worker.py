"""Dedicated worker for provider-event trigger reconciliation."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.observability.provider_trigger_metrics import (
    set_dispatch_backlog_age_seconds,
    set_worker_heartbeat_age_seconds,
)
from orchestra.services.provider_event_blob_cleanup_service import (
    ProviderEventBlobCleanupService,
)
from orchestra.services.provider_event_context_service import (
    ProviderEventContextService,
)
from orchestra.services.provider_event_dispatch_delivery_service import (
    ProviderEventDispatchDeliveryService,
)
from orchestra.services.provider_trigger_reconciliation_service import (
    ProviderTriggerReconciliationService,
)
from orchestra.settings import settings
from orchestra.web.lifetime import create_database_engine, get_engine
from orchestra.workers.provider_trigger_readiness import (
    ProviderTriggerWorkerReadinessServer,
    default_readiness_evaluator,
)

logger = logging.getLogger(__name__)

WORKER_KEY = "provider-trigger-worker"
_shutdown_requested = threading.Event()
_last_cycle_completed_at: datetime | None = None
_last_cycle_error: str | None = None


def _handle_shutdown(signum: int, _frame: object) -> None:
    del signum
    _shutdown_requested.set()


def run_worker_cycle(
    *,
    lease_owner: str | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> dict[str, object]:
    """Run one reconcile, generation, health, and dispatch cycle."""

    global _last_cycle_completed_at, _last_cycle_error

    resolved_owner = lease_owner or f"trigger-worker-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    totals: dict[str, object] = {
        "bindings_claimed": 0,
        "bindings_processed": 0,
        "generations_claimed": 0,
        "generations_processed": 0,
        "bindings_checked": 0,
        "dispatches_claimed": 0,
        "dispatches_delivered": 0,
        "dispatches_started": 0,
        "dispatches_retryable": 0,
        "dispatches_failed": 0,
        "dispatches_duplicate_prevented": 0,
        "dispatches_converged": 0,
        "dispatches_still_in_flight": 0,
        "dispatches_terminal_failed": 0,
        "dispatches_terminal_succeeded": 0,
        "dispatch_backlog_oldest_age_seconds": None,
        "blob_deletions_processed": 0,
        "blob_orphans_removed": 0,
        "event_contexts_expired": 0,
        "last_reconcile_progress_at": None,
        "last_generation_progress_at": None,
        "last_health_progress_at": None,
        "last_dispatch_progress_at": None,
        "last_cleanup_progress_at": None,
    }
    last_reconcile_at = None
    last_health_at = None
    try:
        resolved_session_factory = session_factory or sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
        )
        with resolved_session_factory() as session:
            service = ProviderTriggerReconciliationService(
                session,
                lease_owner=resolved_owner,
            )
            reconcile_stats = service.process_reconcile_batch()
            totals.update(reconcile_stats)
            totals["last_reconcile_progress_at"] = now.isoformat()
            last_reconcile_at = now

            generation_stats = service.process_generation_batch()
            totals.update(generation_stats)
            totals["last_generation_progress_at"] = now.isoformat()

            health_stats = service.process_health_batch()
            totals.update(health_stats)
            totals["last_health_progress_at"] = now.isoformat()
            last_health_at = now

            dispatch_service = ProviderEventDispatchDeliveryService(
                session,
                lease_owner=resolved_owner,
            )
            dispatch_stats = dispatch_service.process_dispatch_batch()
            converge_stats = dispatch_service.process_status_convergence_batch()
            totals.update(dispatch_stats)
            totals.update(converge_stats)
            backlog_age = dispatch_service.backlog_oldest_age_seconds()
            totals["dispatch_backlog_oldest_age_seconds"] = backlog_age
            totals["last_dispatch_progress_at"] = now.isoformat()
            set_dispatch_backlog_age_seconds(backlog_age)

            cleanup_service = ProviderEventBlobCleanupService(session)
            totals["blob_deletions_processed"] = (
                cleanup_service.process_deletion_batch()
            )
            totals["blob_orphans_removed"] = cleanup_service.sweep_orphan_uncommitted()
            totals["event_contexts_expired"] = ProviderEventContextService(
                session,
            ).sweep_expired_contexts()
            totals["last_cleanup_progress_at"] = now.isoformat()

            dao = ProviderTriggerDAO(session)
            dao.record_worker_heartbeat(
                worker_key=WORKER_KEY,
                lease_owner=resolved_owner,
                last_reconcile_at=last_reconcile_at,
                last_health_at=last_health_at,
                metadata=totals,
            )
            session.commit()
        _last_cycle_completed_at = datetime.now(timezone.utc)
        _last_cycle_error = None
        set_worker_heartbeat_age_seconds(0.0)
    except Exception as exc:
        _last_cycle_error = str(exc)
        raise
    return totals


def run_worker_loop(*, once: bool = False) -> None:
    """Run the provider-trigger worker until shutdown or one-shot completion."""

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    lease_owner = f"trigger-worker-{uuid.uuid4().hex[:12]}"
    engine = create_database_engine()
    worker_session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    readiness_server: ProviderTriggerWorkerReadinessServer | None = None

    try:
        if not once and os.environ.get("PROVIDER_TRIGGER_WORKER_READINESS", "1") == "1":
            readiness_server = ProviderTriggerWorkerReadinessServer(
                host="0.0.0.0",
                port=settings.provider_trigger_worker_readiness_port,
                evaluator=lambda: default_readiness_evaluator(
                    last_cycle_completed_at=_last_cycle_completed_at,
                    max_age_seconds=settings.provider_trigger_worker_heartbeat_max_age_seconds,
                    last_cycle_error=_last_cycle_error,
                ),
            )
            readiness_server.start()

        while not _shutdown_requested.is_set():
            started = time.monotonic()
            stats = run_worker_cycle(
                lease_owner=lease_owner,
                session_factory=worker_session_factory,
            )
            logger.info("provider-trigger worker cycle complete stats=%s", stats)

            if once:
                return

            elapsed = time.monotonic() - started
            sleep_seconds = max(
                1.0,
                settings.provider_trigger_reconcile_interval_seconds - elapsed,
            )
            _shutdown_requested.wait(sleep_seconds)
    finally:
        if readiness_server is not None:
            readiness_server.stop()
        engine.dispose()


def main() -> None:
    """CLI entrypoint for the provider-trigger worker."""

    parser = argparse.ArgumentParser(
        description="Provider-trigger reconciliation worker",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one reconcile/generation/health cycle and exit.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run_worker_loop(once=args.once)


if __name__ == "__main__":
    main()
