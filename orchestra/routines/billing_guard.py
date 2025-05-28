"""Suspend users that remain PAST_DUE and have no credits left.

Runs once per day via scheduled job.
"""

from __future__ import annotations

import logging

from orchestra.db.models.orchestra_models import Users as User
from orchestra.db.session import SessionLocal
from orchestra.observability.metrics import billing_suspended_total

logger = logging.getLogger(__name__)


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
