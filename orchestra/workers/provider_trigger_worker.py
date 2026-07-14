"""Dedicated worker for provider-event trigger reconciliation."""

from __future__ import annotations

import argparse
import logging
import signal
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.services.provider_event_dispatch_delivery_service import (
    ProviderEventDispatchDeliveryService,
)
from orchestra.services.provider_trigger_reconciliation_service import (
    ProviderTriggerReconciliationService,
)
from orchestra.settings import settings
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)

WORKER_KEY = "provider-trigger-worker"
_shutdown_requested = False


def _handle_shutdown(signum: int, _frame: object) -> None:
    del signum
    global _shutdown_requested
    _shutdown_requested = True


def run_worker_cycle(
    *,
    lease_owner: str | None = None,
) -> dict[str, int]:
    """Run one reconcile, generation, health, and dispatch cycle."""

    resolved_owner = lease_owner or f"trigger-worker-{uuid.uuid4().hex[:8]}"
    totals = {
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
    }
    with sessionmaker(bind=get_engine(), expire_on_commit=False)() as session:
        service = ProviderTriggerReconciliationService(
            session,
            lease_owner=resolved_owner,
        )
        reconcile_stats = service.process_reconcile_batch()
        generation_stats = service.process_generation_batch()
        health_stats = service.process_health_batch()
        totals.update(reconcile_stats)
        totals.update(generation_stats)
        totals.update(health_stats)

        dispatch_service = ProviderEventDispatchDeliveryService(
            session,
            lease_owner=resolved_owner,
        )
        dispatch_stats = dispatch_service.process_dispatch_batch()
        converge_stats = dispatch_service.process_status_convergence_batch()
        totals.update(dispatch_stats)
        totals.update(converge_stats)
        totals["dispatch_backlog_oldest_age_seconds"] = (
            dispatch_service.backlog_oldest_age_seconds()
        )

        dao = ProviderTriggerDAO(session)
        dao.record_worker_heartbeat(
            worker_key=WORKER_KEY,
            lease_owner=resolved_owner,
            last_reconcile_at=datetime.now(timezone.utc),
            last_health_at=datetime.now(timezone.utc),
            metadata=totals,
        )
        session.commit()
    return totals


def run_worker_loop(*, once: bool = False) -> None:
    """Run the provider-trigger worker until shutdown or one-shot completion."""

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    last_health_at = 0.0
    while not _shutdown_requested:
        started = time.monotonic()
        stats = run_worker_cycle()
        logger.info("provider-trigger worker cycle complete stats=%s", stats)

        if once:
            return

        elapsed = time.monotonic() - started
        health_due = (
            time.monotonic() - last_health_at
            >= settings.provider_trigger_health_interval_seconds
        )
        if health_due:
            last_health_at = time.monotonic()

        sleep_seconds = max(
            1.0,
            settings.provider_trigger_reconcile_interval_seconds - elapsed,
        )
        time.sleep(sleep_seconds)


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
