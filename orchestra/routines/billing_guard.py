"""Suspend billing accounts that remain PAST_DUE and have no credits left.

Runs once per day via scheduled job.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import BillingAccount
from orchestra.web.api.utils.prometheus_middleware import BILLING_SUSPENDED_TOTAL
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


def suspend_past_due_accounts(session: Optional[Session] = None) -> None:
    """PUT any 'PAST_DUE & empty-wallet' billing accounts into SUSPENDED."""
    if session is not None:
        _suspend_accounts_in_session(session)
    else:
        SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
        with SessionLocal() as session:
            _suspend_accounts_in_session(session)


def _suspend_accounts_in_session(session: Session) -> None:
    """Internal function to suspend billing accounts within a given session."""
    try:
        # Fetch the accounts that match, update them in-place, and collect
        # IDs for metrics — all in a single query.
        accounts_to_suspend = (
            session.query(BillingAccount)
            .filter(
                BillingAccount.account_status == "PAST_DUE",
                BillingAccount.credits <= 0,
            )
            .all()
        )

        for ba in accounts_to_suspend:
            ba.account_status = "SUSPENDED"

        session.commit()

        # Increment metrics for each suspended account
        for ba in accounts_to_suspend:
            BILLING_SUSPENDED_TOTAL.labels(billing_account_id=str(ba.id)).inc()

    except Exception:  # noqa: BLE001  (propagate)
        session.rollback()
        raise

    logger.info(
        "Billing-guard: suspended %s billing account(s) for non-payment",
        len(accounts_to_suspend),
    )
