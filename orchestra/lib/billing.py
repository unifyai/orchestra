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

    # Create the database record first
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

    # Now create the Stripe invoice item if user has a Stripe customer ID
    if user.stripe_customer_id:
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

            # Create Stripe invoice item
            print(
                f"[AUTO-RECHARGE] Calling Stripe API - Customer: {user.stripe_customer_id}, "
                f"Amount: ${credits} ({credits * 100} cents)",
            )

            invoice_item = stripe.InvoiceItem.create(
                customer=user.stripe_customer_id,
                amount=int(credits * 100),  # Convert to cents
                currency="usd",
                description=f"{credits} credits (auto-recharge)",
                metadata={
                    "recharge_type": "auto",
                    "user_id": user.id,
                    "invoice_group": str(invoice_group),
                },
            )

            print(
                f"[AUTO-RECHARGE] SUCCESS: Stripe invoice item created - "
                f"Invoice Item ID: {invoice_item.id}, "
                f"Customer: {invoice_item.customer}, "
                f"Amount: {invoice_item.amount} cents",
            )

        except stripe.error.StripeError as e:
            print(
                f"[AUTO-RECHARGE] STRIPE ERROR: {type(e).__name__} - "
                f"Message: {str(e)}, "
                f"Code: {getattr(e, 'code', 'N/A')}",
            )
            # Don't raise - we still want the recharge record in the database

        except Exception as e:
            print(
                f"[AUTO-RECHARGE] UNEXPECTED ERROR: {type(e).__name__} - "
                f"Message: {str(e)}",
            )
            # Don't raise - we still want the recharge record in the database
    else:
        print(f"[AUTO-RECHARGE] WARNING: User {user.id} has no Stripe customer ID")
