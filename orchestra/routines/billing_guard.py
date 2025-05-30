"""Suspend users that remain PAST_DUE and have no credits left.

Runs once per day via scheduled job.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import sessionmaker

from orchestra.db.models.orchestra_models import Users as User
from orchestra.observability.metrics import billing_suspended_total
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


def suspend_past_due_users() -> None:
    """PUT any 'PAST_DUE & empty-wallet' accounts into SUSPENDED."""
    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        try:
            # First, get the users that will be suspended
            users_to_suspend = (
                session.query(User)
                .filter(
                    User.billing_state == "PAST_DUE",
                    User.credits <= 0,
                )
                .all()
            )

            # Update their billing state
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

            # Increment metrics for each suspended user
            for user in users_to_suspend:
                billing_suspended_total.labels(user_id=user.id).inc()

        except Exception:  # noqa: BLE001  (propagate)
            session.rollback()
            raise

        logger.info("Billing-guard: suspended %s user(s) for non-payment", count)
