"""Shared billing utilities for Orchestra."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    Recharge,
    RechargeStatus,
)
from orchestra.db.models.orchestra_models import Users as User

logger = logging.getLogger(__name__)


def credits_to_usd(credits: int) -> Decimal:
    """Convert credits to USD amount.

    Args:
        credits: Number of credits to convert

    Returns:
        USD amount as Decimal (rate: $0.01 per credit)
    """
    return Decimal(credits) * Decimal("0.01")


def _month_end_utc(ts: datetime | None = None) -> datetime:
    """Return the 23:59:59.999 of the month that `ts` falls in (UTC)."""
    if ts is None:
        ts = datetime.now(timezone.utc)

    first_next_month = (ts.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_next_month - timedelta(days=1)


def queue_auto_recharge(session: Session, user: User, credits: int) -> None:
    """
    Queue an auto-recharge for monthly invoicing.

    This function records a recharge row without calling Stripe directly.
    The monthly invoicer will aggregate these into bulk invoices.

    Args:
        session: Database session
        user: User object to recharge
        credits: Number of credits to recharge
    """
    logger.info(f"Queueing auto-recharge for user {user.id}: {credits} credits")

    recharge = Recharge(
        user_id=user.id,
        type=RECHARGE_TYPE_AUTO,
        quantity=Decimal(credits),
        amount_usd=credits_to_usd(credits),
        invoice_group=_month_end_utc().date(),
        status=RechargeStatus.PENDING_INVOICE,
    )

    session.add(recharge)
    logger.info(
        f"Auto-recharge queued for user {user.id}: ${credits_to_usd(credits):.2f}",
    )
