"""Suspend billing accounts that remain PAST_DUE and have no credits left.

Runs once per day via scheduled job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import BillingAccount
from orchestra.web.api.utils.prometheus_middleware import BILLING_SUSPENDED_TOTAL
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


@dataclass
class GuardResult:
    """Summary of a billing-guard run."""

    accounts_suspended: int = 0
    accounts_healed_past_due: int = 0
    accounts_failed: int = 0
    failed_ids: List[int] = field(default_factory=list)


def suspend_past_due_accounts(
    session: Optional[Session] = None,
) -> GuardResult:
    """Suspend PAST_DUE accounts with zero or negative credits.

    Each account is processed independently so that a failure on one
    does not prevent others from being suspended.

    Returns a :class:`GuardResult` summarising what happened.
    """
    if session is not None:
        return _suspend_accounts_in_session(session)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        return _suspend_accounts_in_session(session)


def _suspend_accounts_in_session(session: Session) -> GuardResult:
    result = GuardResult()

    # ── Self-heal: ACTIVE accounts with negative credits → PAST_DUE ──
    # This catches cases where a webhook was missed or a levy pushed
    # credits negative without the status being updated.
    active_negative = (
        session.query(BillingAccount)
        .filter(
            BillingAccount.account_status == "ACTIVE",
            BillingAccount.credits < 0,
        )
        .all()
    )

    for ba in active_negative:
        try:
            ba.account_status = "PAST_DUE"
            session.commit()

            result.accounts_healed_past_due += 1
            logger.warning(
                {
                    "message": "Self-healed ACTIVE account with negative credits to PAST_DUE",
                    "billing_account_id": ba.id,
                    "credits": float(ba.credits),
                },
            )
        except Exception:
            session.rollback()
            result.accounts_failed += 1
            result.failed_ids.append(ba.id)
            logger.exception(
                {
                    "message": "Failed to heal ACTIVE negative-balance account",
                    "billing_account_id": ba.id,
                },
            )

    # ── PAST_DUE + zero/negative credits → SUSPENDED ─────────────────
    accounts_to_suspend = (
        session.query(BillingAccount)
        .filter(
            BillingAccount.account_status == "PAST_DUE",
            BillingAccount.credits <= 0,
        )
        .all()
    )

    for ba in accounts_to_suspend:
        try:
            ba.account_status = "SUSPENDED"
            ba.autorecharge = False
            session.commit()

            BILLING_SUSPENDED_TOTAL.labels(billing_account_id=str(ba.id)).inc()
            result.accounts_suspended += 1

            logger.info(
                {
                    "message": "Billing account suspended by guard",
                    "billing_account_id": ba.id,
                    "credits": float(ba.credits),
                },
            )
        except Exception:
            session.rollback()
            result.accounts_failed += 1
            result.failed_ids.append(ba.id)
            logger.exception(
                {
                    "message": "Failed to suspend billing account",
                    "billing_account_id": ba.id,
                },
            )

    logger.info(
        {
            "message": "Billing-guard run complete",
            "accounts_suspended": result.accounts_suspended,
            "accounts_healed_past_due": result.accounts_healed_past_due,
            "accounts_failed": result.accounts_failed,
        },
    )

    return result
