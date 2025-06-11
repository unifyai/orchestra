"""Shared billing utilities for Orchestra."""

import logging
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

logger = logging.getLogger(__name__)

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
    logger.info(
        f"Queueing auto-recharge - User ID: {user.id}, "
        f"Credits: {credits}, "
        f"Stripe Customer ID: {user.stripe_customer_id}",
    )

    now = datetime.now(timezone.utc)
    invoice_group = month_end_utc(now)

    logger.info(
        f"Creating recharge record - "
        f"Type: {RECHARGE_TYPE_AUTO}, "
        f"Status: {RechargeStatus.PENDING_INVOICE}, "
        f"Invoice group: {invoice_group}",
    )

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

    logger.info(
        f"Auto-recharge record created for user {user.id}: "
        f"${credits:.2f} ({credits} credits), "
        f"Invoice group: {invoice_group}",
    )

    # Now create the Stripe invoice item if user has a Stripe customer ID
    if user.stripe_customer_id:
        logger.info(f"Creating Stripe invoice item for user {user.id}")

        try:
            # Configure Stripe API key
            stripe_key = os.environ.get("STRIPE_SECRET_KEY")
            if not stripe_key:
                logger.error(
                    "STRIPE_SECRET_KEY environment variable not set - cannot create Stripe invoice item",
                )
                return

            stripe.api_key = stripe_key
            logger.info("Stripe API key configured successfully")

            # Create Stripe invoice item
            logger.info(
                f"Calling Stripe API - Customer: {user.stripe_customer_id}, "
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

            logger.info(
                f"Stripe invoice item created successfully - "
                f"Invoice Item ID: {invoice_item.id}, "
                f"Customer: {invoice_item.customer}, "
                f"Amount: {invoice_item.amount} cents",
            )

        except stripe.error.StripeError as e:
            logger.error(
                f"Stripe API error creating invoice item for auto-recharge - "
                f"User: {user.id}, "
                f"Type: {type(e).__name__}, "
                f"Message: {str(e)}, "
                f"Code: {getattr(e, 'code', 'N/A')}, "
                f"Param: {getattr(e, 'param', 'N/A')}",
            )
            # Don't raise - we still want the recharge record in the database

        except Exception as e:
            logger.error(
                f"Unexpected error creating Stripe invoice item for auto-recharge - "
                f"User: {user.id}, "
                f"Type: {type(e).__name__}, "
                f"Message: {str(e)}",
            )
            # Don't raise - we still want the recharge record in the database
    else:
        logger.warning(
            f"User {user.id} has no Stripe customer ID - "
            f"cannot create Stripe invoice item for auto-recharge",
        )
