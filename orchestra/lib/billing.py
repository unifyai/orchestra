"""Shared billing utilities for Orchestra."""

import os
from datetime import datetime, timezone
from decimal import Decimal

import stripe
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    Recharge,
    RechargeStatus,
)
from orchestra.db.models.orchestra_models import Users as User
from orchestra.lib.time import month_end_utc

# Stripe product configuration for Unify Credits
# This ensures consistent 1:1 pricing (1 credit = $1) throughout the system
UNIFY_CREDITS_PRICE_ID = "price_1Oyd7CKWSwIeRavwlfMp1uGp"
UNIFY_CREDITS_PRODUCT_ID = "prod_PoFcbDHMdLYNH5"


def queue_auto_recharge(session: Session, user: User, credits: int) -> None:
    """
    Queue an auto-recharge for monthly invoicing AND create Stripe invoice item.

    This function:
    1. Records a recharge row in the database
    2. Creates a Stripe invoice item immediately (for later invoicing)

    Args:
        session: Database session
        user: User object to recharge
        credits: Number of credits to recharge
    """
    if not user.stripe_customer_id:
        # Log an error and abort. Do not create a recharge record.
        print(
            f"[AUTO-RECHARGE] ERROR: Attempted to auto-recharge for user {user.id} "
            "who has no Stripe customer ID. Aborting.",
        )
        raise ValueError(f"Cannot auto-recharge user {user.id} without a Stripe ID.")

    print(
        f"[AUTO-RECHARGE] Queueing auto-recharge - User ID: {user.id}, "
        f"Credits: {credits}, "
        f"Stripe Customer ID: {user.stripe_customer_id}",
    )

    now = datetime.now(timezone.utc)
    invoice_group = month_end_utc(now)

    # Validate Stripe customer & default payment method BEFORE creating any DB record
    print(f"[AUTO-RECHARGE] Creating Stripe invoice item for user {user.id}")

    try:
        # Configure Stripe API key
        stripe_key = os.environ.get("STRIPE_SECRET_KEY")
        if not stripe_key:
            print(
                "[AUTO-RECHARGE] ERROR: STRIPE_SECRET_KEY environment variable not set!",
            )
            return

        stripe.api_key = stripe_key
        print(
            f"[AUTO-RECHARGE] Stripe API key configured (key prefix: {stripe_key[:10]}...)",
        )

        # Retrieve customer and ensure default payment method BEFORE any further Stripe calls
        customer = stripe.Customer.retrieve(user.stripe_customer_id)

        default_pm = (
            customer.get("invoice_settings", {}).get("default_payment_method")
            if customer
            else None
        )

        if not default_pm:
            raise ValueError(
                f"Customer {user.stripe_customer_id} has no default payment method configured",
            )

        # --- Passed all validation; create DB recharge record now ---
        recharge = Recharge(
            user_id=user.id,
            type=RECHARGE_TYPE_AUTO,
            quantity=Decimal(credits),
            amount_usd=Decimal(credits),  # 1 credit = $1
            invoice_group=invoice_group,
            status=RechargeStatus.PENDING_INVOICE,
        )

        session.add(recharge)
        print(
            f"[AUTO-RECHARGE] Auto-recharge record created for user {user.id}: "
            f"${credits:.2f} ({credits} credits), "
            f"Invoice group: {invoice_group}",
        )

    except stripe.error.StripeError as e:
        print(
            f"[AUTO-RECHARGE] STRIPE ERROR: {type(e).__name__} - "
            f"Message: {str(e)}, "
            f"Code: {getattr(e, 'code', 'N/A')}",
        )
        # Keep database record, but propagate error so caller can handle if needed
        raise
