"""Refresh provider-trigger Prometheus gauges from durable worker state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.observability.provider_trigger_metrics import (
    set_dispatch_backlog_age_seconds,
    set_reconcile_backlog_age_seconds,
    set_worker_heartbeat_age_seconds,
)
from orchestra.services.provider_event_dispatch_delivery_service import (
    ProviderEventDispatchDeliveryService,
)
from orchestra.web.lifetime import get_engine
from orchestra.workers.provider_trigger_worker import WORKER_KEY

logger = logging.getLogger(__name__)


def refresh_provider_trigger_metrics() -> None:
    """Publish provider-trigger topology gauges from the database."""

    try:
        with sessionmaker(bind=get_engine(), expire_on_commit=False)() as session:
            heartbeat = ProviderTriggerDAO(session).get_worker_heartbeat(
                worker_key=WORKER_KEY,
            )
            if heartbeat is None or heartbeat.last_heartbeat_at is None:
                set_worker_heartbeat_age_seconds(None)
            else:
                now = datetime.now(timezone.utc)
                last_seen = heartbeat.last_heartbeat_at
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                set_worker_heartbeat_age_seconds((now - last_seen).total_seconds())

            backlog_age = ProviderEventDispatchDeliveryService(
                session,
            ).backlog_oldest_age_seconds()
            set_dispatch_backlog_age_seconds(backlog_age)

            reconcile_backlog = ProviderTriggerDAO(session).list_reconcile_backlog(
                limit=1,
            )
            if not reconcile_backlog:
                set_reconcile_backlog_age_seconds(0)
            else:
                oldest_retry = reconcile_backlog[0].reconcile_next_retry_at
                if oldest_retry is None:
                    set_reconcile_backlog_age_seconds(0)
                else:
                    if oldest_retry.tzinfo is None:
                        oldest_retry = oldest_retry.replace(tzinfo=timezone.utc)
                    set_reconcile_backlog_age_seconds(
                        max(
                            0,
                            int(
                                (
                                    datetime.now(timezone.utc)
                                    - oldest_retry.astimezone(timezone.utc)
                                ).total_seconds(),
                            ),
                        ),
                    )
    except Exception:
        logger.exception("failed to refresh provider-trigger metrics")
