"""Shared billing utilities for Orchestra."""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    Recharge,
    RechargeStatus,
)
from orchestra.db.models.orchestra_models import Users as User
from orchestra.lib.time import month_end_utc

logger = logging.getLogger(__name__)


def credits_to_usd(credits: int) -> Decimal:
    """Convert credits to USD amount."""
    return Decimal(credits) * Decimal("0.01")


def get_appropriate_stripe_key() -> str | None:
    """
    Get the appropriate Stripe API key based on environment.

    Priority order:
    1. STRIPE_SECRET_KEY_TEST (for testing environments)
    2. STRIPE_SECRET_KEY_LIVE (for production)

    Returns:
        The appropriate Stripe API key, or None if no valid key is found.
    """
    # Check for test key first (safer for development/testing)
    test_key = os.environ.get("STRIPE_SECRET_KEY")
    if test_key and test_key.startswith("sk_test_"):
        return test_key

    # Fall back to live key for production
    live_key = os.environ.get("STRIPE_SECRET_KEY_LIVE")
    if live_key and live_key.startswith("sk_live_"):
        return live_key

    return None


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
        invoice_group=month_end_utc(datetime.now(timezone.utc)),
        status=RechargeStatus.PENDING_INVOICE,
    )

    session.add(recharge)
    logger.info(
        f"Auto-recharge queued for user {user.id}: ${credits_to_usd(credits):.2f}",
    )
