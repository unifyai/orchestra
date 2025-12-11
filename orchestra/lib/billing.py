"""Shared billing utilities for Orchestra."""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Union

import stripe
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    Organization,
    Recharge,
    RechargeStatus,
)
from orchestra.db.models.orchestra_models import Users as User
from orchestra.lib.time import month_end_utc

# Stripe product configuration for Unify Credits
# This ensures consistent 1:1 pricing (1 credit = $1) throughout the system
UNIFY_CREDITS_PRICE_ID = "price_1Oyd7CKWSwIeRavwlfMp1uGp"
UNIFY_CREDITS_PRODUCT_ID = "prod_PoFcbDHMdLYNH5"


class BillingEntityType(str, Enum):
    """Type of billing entity."""

    USER = "user"
    ORGANIZATION = "organization"


@dataclass
class BillingEntity:
    """
    Represents the entity responsible for billing.

    This can be either a user (for personal accounts or delegated org billing)
    or an organization (for direct org billing).
    """

    entity_type: BillingEntityType
    entity_id: Union[str, int]  # str for user_id, int for organization_id
    credits: Decimal
    stripe_customer_id: Optional[str]
    autorecharge: bool
    autorecharge_threshold: Decimal
    autorecharge_qty: Decimal

    @property
    def is_user(self) -> bool:
        """Check if this is a user billing entity."""
        return self.entity_type == BillingEntityType.USER

    @property
    def is_organization(self) -> bool:
        """Check if this is an organization billing entity."""
        return self.entity_type == BillingEntityType.ORGANIZATION

    @property
    def has_direct_billing(self) -> bool:
        """Check if entity has Stripe customer ID for direct billing."""
        return self.stripe_customer_id is not None

    def should_trigger_autorecharge(self, new_balance: Decimal) -> bool:
        """
        Check if autorecharge should be triggered after a deduction.

        Args:
            new_balance: The credit balance after deduction.

        Returns:
            True if autorecharge should be triggered.
        """
        if not self.autorecharge:
            return False
        if not self.has_direct_billing:
            return False
        return new_balance <= self.autorecharge_threshold


def get_billing_entity(
    session: Session,
    user_id: str,
    organization_id: Optional[int] = None,
) -> BillingEntity:
    """
    Get the billing entity for a given request context.

    This function determines who should be billed based on:
    - Personal context (organization_id is None): Bill the user directly
    - Org context with direct billing (stripe_customer_id set): Bill the org
    - Org context with delegated billing: Bill the org's billing_user_id

    Args:
        session: Database session.
        user_id: The ID of the user making the request.
        organization_id: The organization context (None = personal).

    Returns:
        BillingEntity containing billing information.

    Raises:
        ValueError: If organization not found or billing user not found.
    """
    # Personal query - bill the user directly
    if organization_id is None:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            raise ValueError(f"User with id {user_id} not found.")

        return BillingEntity(
            entity_type=BillingEntityType.USER,
            entity_id=user.id,
            credits=user.credits,
            stripe_customer_id=user.stripe_customer_id,
            autorecharge=user.autorecharge,
            autorecharge_threshold=user.autorecharge_threshold,
            autorecharge_qty=user.autorecharge_qty,
        )

    # Organization context
    org = session.query(Organization).filter_by(id=organization_id).first()
    if not org:
        raise ValueError(f"Organization with id {organization_id} not found.")

    # Check if organization is active (frozen/suspended orgs cannot spend credits)
    if org.account_status != "ACTIVE":
        raise ValueError(
            f"Organization {organization_id} is {org.account_status}. "
            f"Billing operations not allowed for non-active organizations.",
        )

    # Check if org has direct billing (has its own stripe_customer_id)
    if org.stripe_customer_id is not None:
        # Direct organization billing
        return BillingEntity(
            entity_type=BillingEntityType.ORGANIZATION,
            entity_id=org.id,
            credits=org.credits,
            stripe_customer_id=org.stripe_customer_id,
            autorecharge=org.autorecharge,
            autorecharge_threshold=org.autorecharge_threshold,
            autorecharge_qty=org.autorecharge_qty,
        )

    # Delegated billing - bill the org's billing user
    if not org.billing_user_id:
        raise ValueError(
            f"Organization {organization_id} has no billing_user_id and no direct billing.",
        )

    billing_user = session.query(User).filter_by(id=org.billing_user_id).first()
    if not billing_user:
        raise ValueError(
            f"Billing user {org.billing_user_id} for organization "
            f"{organization_id} not found.",
        )

    return BillingEntity(
        entity_type=BillingEntityType.USER,
        entity_id=billing_user.id,
        credits=billing_user.credits,
        stripe_customer_id=billing_user.stripe_customer_id,
        autorecharge=billing_user.autorecharge,
        autorecharge_threshold=billing_user.autorecharge_threshold,
        autorecharge_qty=billing_user.autorecharge_qty,
    )


def deduct_credits(
    session: Session,
    billing_entity: BillingEntity,
    amount: Decimal,
) -> Decimal:
    """
    Deduct credits from a billing entity.

    Args:
        session: Database session.
        billing_entity: The entity to deduct from.
        amount: Amount of credits to deduct.

    Returns:
        The new credit balance after deduction.

    Raises:
        ValueError: If entity not found.
    """
    if billing_entity.is_user:
        user = session.query(User).filter_by(id=billing_entity.entity_id).first()
        if not user:
            raise ValueError(f"User {billing_entity.entity_id} not found.")

        user.credits = user.credits - amount
        return user.credits

    else:  # Organization
        org = session.query(Organization).filter_by(
            id=billing_entity.entity_id,
        ).first()
        if not org:
            raise ValueError(f"Organization {billing_entity.entity_id} not found.")

        org.credits = org.credits - amount
        return org.credits


def queue_org_auto_recharge(
    session: Session,
    org: Organization,
    credits: int,
) -> None:
    """
    Queue an auto-recharge for an organization with direct billing.

    Similar to queue_auto_recharge but for organizations.

    Args:
        session: Database session.
        org: Organization object to recharge.
        credits: Number of credits to recharge.
    """
    print(
        f"[ORG-AUTO-RECHARGE] Queueing org auto-recharge - Org ID: {org.id}, "
        f"Credits: {credits}, "
        f"Stripe Customer ID: {org.stripe_customer_id}",
    )

    now = datetime.now(timezone.utc)
    invoice_group = month_end_utc(now)

    # Create the database record for organization recharge
    recharge = Recharge(
        organization_id=org.id,
        user_id=None,  # Organization recharge, not user
        type=RECHARGE_TYPE_AUTO,
        quantity=Decimal(credits),
        amount_usd=Decimal(credits),  # 1 credit = $1
        invoice_group=invoice_group,
        status=RechargeStatus.PENDING_INVOICE,
    )

    session.add(recharge)

    print(
        f"[ORG-AUTO-RECHARGE] Auto-recharge record created for org {org.id}: "
        f"${credits:.2f} ({credits} credits), "
        f"Invoice group: {invoice_group}",
    )

    # Create Stripe invoice item if org has Stripe customer ID
    if org.stripe_customer_id:
        print(f"[ORG-AUTO-RECHARGE] Creating Stripe invoice item for org {org.id}")

        try:
            stripe_key = os.environ.get("STRIPE_SECRET_KEY")
            if not stripe_key:
                print(
                    "[ORG-AUTO-RECHARGE] ERROR: STRIPE_SECRET_KEY not set!",
                )
                return

            stripe.api_key = stripe_key

            invoice_item = stripe.InvoiceItem.create(
                customer=org.stripe_customer_id,
                amount=int(credits * 100),  # Convert to cents
                currency="usd",
                description=f"{credits} credits (auto-recharge)",
                metadata={
                    "recharge_type": "auto",
                    "organization_id": str(org.id),
                    "invoice_group": str(invoice_group),
                },
            )

            print(
                f"[ORG-AUTO-RECHARGE] SUCCESS: Stripe invoice item created - "
                f"Invoice Item ID: {invoice_item.id}",
            )

        except stripe.error.StripeError as e:
            print(
                f"[ORG-AUTO-RECHARGE] STRIPE ERROR: {type(e).__name__} - {str(e)}",
            )

        except Exception as e:
            print(
                f"[ORG-AUTO-RECHARGE] UNEXPECTED ERROR: {type(e).__name__} - {str(e)}",
            )
    else:
        print(f"[ORG-AUTO-RECHARGE] WARNING: Org {org.id} has no Stripe customer ID")


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


def get_billing_user_id(
    session: Session,
    user_id: str,
    organization_id: Optional[int] = None,
) -> str:
    """
    Determine which user account should be billed for a query.

    For personal queries (organization_id is None):
        - Bill the user directly

    For organizational queries (organization_id is set):
        - Bill the organization's billing_user_id

    Args:
        session: Database session
        user_id: The ID of the user making the request (the actor)
        organization_id: The organization context (None = personal)

    Returns:
        The user ID that should be billed

    Raises:
        ValueError: If organization_id is provided but organization not found
    """
    # Personal query - bill the user directly
    if organization_id is None:
        return user_id

    # Organizational query - bill the organization's billing user
    org = session.query(Organization).filter_by(id=organization_id).first()

    if not org:
        raise ValueError(
            f"Organization with id {organization_id} not found. "
            f"Cannot determine billing user.",
        )

    return org.billing_user_id
