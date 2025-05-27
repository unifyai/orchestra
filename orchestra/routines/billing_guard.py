"""Suspend users that remain PAST_DUE and have no credits left.

Runs once per day via Celery beat.
"""

from __future__ import annotations

import logging

# Don't require Celery for unit-tests
try:
    from celery import shared_task  # type: ignore
except ModuleNotFoundError:  # pragma: no cover

    def shared_task(fn=None, **_kw):  # type: ignore
        """No-op decorator replacing `@shared_task` when Celery is absent."""
        if fn is None:
            return lambda f: f
        return fn


from orchestra.db.models.orchestra_models import Users as User
from orchestra.db.session import SessionLocal
from orchestra.observability.metrics import billing_suspended_total

logger = logging.getLogger(__name__)


@shared_task
def suspend_past_due_users() -> None:
    """PUT any 'PAST_DUE & empty-wallet' accounts into SUSPENDED."""
    with SessionLocal() as session:
        try:
            count = (
                session.query(User)
                .filter(
                    User.billing_state == "PAST_DUE",
                    User.credits <= 0,
                )
                .update(
                    {"billing_state": "SUSPENDED"},
                    synchronize_session=False,
                )
            )
            session.commit()
        except Exception:  # noqa: BLE001  (propagate)
            session.rollback()
            raise

        billing_suspended_total.inc(count)
        logger.info("Billing-guard: suspended %s user(s) for non-payment", count)
