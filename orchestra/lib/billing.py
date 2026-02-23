"""Shared billing utilities for Orchestra.

All billing operations now operate through BillingAccount.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Union

import stripe
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    BillingAccount,
    Organization,
    Recharge,
    RechargeStatus,
    User,
)
from orchestra.lib.time import month_end_utc
from orchestra.settings import settings


class BillingEntityType(str, Enum):
    """Type of billing entity."""

    USER = "user"
    ORGANIZATION = "organization"


@dataclass
class BillingEntity:
    """
    Represents the entity responsible for billing.

    This can be either a user (for personal accounts) or an organization
    (for direct org billing). The billing_account holds all billing state.
    """

    entity_type: BillingEntityType
    entity_id: Union[str, int]  # str for user_id, int for organization_id
    billing_account_id: int
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
    def has_billing(self) -> bool:
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
        if not self.has_billing:
            return False
        return new_balance <= self.autorecharge_threshold


def _get_billing_account_for_user(
    session: Session,
    user: User,
) -> BillingAccount:
    """Get or raise for a user's billing account."""
    if user.billing_account_id is None:
        raise ValueError(
            f"User {user.id} has no billing account set up.",
        )
    ba = (
        session.query(BillingAccount)
        .filter(BillingAccount.id == user.billing_account_id)
        .first()
    )
    if ba is None:
        raise ValueError(
            f"BillingAccount {user.billing_account_id} not found for user {user.id}.",
        )
    return ba


def _get_billing_account_for_org(
    session: Session,
    org: Organization,
) -> BillingAccount:
    """Get or raise for an org's billing account."""
    if org.billing_account_id is None:
        raise ValueError(
            f"Organization {org.id} has no billing set up. "
            f"Please set up billing in the organization settings.",
        )
    ba = (
        session.query(BillingAccount)
        .filter(BillingAccount.id == org.billing_account_id)
        .first()
    )
    if ba is None:
        raise ValueError(
            f"BillingAccount {org.billing_account_id} not found for org {org.id}.",
        )
    return ba


def get_billing_entity(
    session: Session,
    user_id: str,
    organization_id: Optional[int] = None,
) -> BillingEntity:
    """
    Get the billing entity for a given request context.

    This function determines who should be billed based on:
    - Personal context (organization_id is None): Bill the user directly
    - Org context with direct billing (billing_account with stripe_customer_id): Bill the org
    - Org context without billing: Raise error (billing not set up)

    Args:
        session: Database session.
        user_id: The ID of the user making the request.
        organization_id: The organization context (None = personal).

    Returns:
        BillingEntity containing billing information.

    Raises:
        ValueError: If entity not found or billing not set up.
    """
    # Personal query - bill the user directly
    if organization_id is None:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            raise ValueError(f"User with id {user_id} not found.")

        ba = _get_billing_account_for_user(session, user)

        return BillingEntity(
            entity_type=BillingEntityType.USER,
            entity_id=user.id,
            billing_account_id=ba.id,
            credits=ba.credits,
            stripe_customer_id=ba.stripe_customer_id,
            autorecharge=ba.autorecharge,
            autorecharge_threshold=ba.autorecharge_threshold,
            autorecharge_qty=ba.autorecharge_qty,
        )

    # Organization context
    org = session.query(Organization).filter_by(id=organization_id).first()
    if not org:
        raise ValueError(f"Organization with id {organization_id} not found.")

    ba = _get_billing_account_for_org(session, org)

    # Check if organization is active
    if ba.account_status != "ACTIVE":
        raise ValueError(
            f"Organization {organization_id} is {ba.account_status}. "
            f"Billing operations not allowed for non-active organizations.",
        )

    # Check if org has direct billing
    if ba.stripe_customer_id is not None:
        return BillingEntity(
            entity_type=BillingEntityType.ORGANIZATION,
            entity_id=org.id,
            billing_account_id=ba.id,
            credits=ba.credits,
            stripe_customer_id=ba.stripe_customer_id,
            autorecharge=ba.autorecharge,
            autorecharge_threshold=ba.autorecharge_threshold,
            autorecharge_qty=ba.autorecharge_qty,
        )

    # Billing not set up for organization
    raise ValueError(
        f"Organization {organization_id} has no billing set up. "
        f"Please set up billing in the organization settings.",
    )


def deduct_credits(
    session: Session,
    billing_entity: BillingEntity,
    amount: Decimal,
) -> Decimal:
    """
    Deduct credits from a billing entity (via BillingAccount).

    Args:
        session: Database session.
        billing_entity: The entity to deduct from.
        amount: Amount of credits to deduct.

    Returns:
        The new credit balance after deduction.

    Raises:
        ValueError: If billing account not found.
    """
    ba = (
        session.query(BillingAccount)
        .filter(BillingAccount.id == billing_entity.billing_account_id)
        .first()
    )
    if not ba:
        raise ValueError(
            f"BillingAccount {billing_entity.billing_account_id} not found.",
        )

    ba.credits = ba.credits - amount
    return ba.credits


def queue_auto_recharge(
    session: Session,
    billing_account: BillingAccount,
    credits: int,
    entity_label: str = "",
) -> None:
    """
    Queue an auto-recharge for a billing account AND create Stripe invoice item.

    Unified function for both user and organization auto-recharge.

    Args:
        session: Database session.
        billing_account: BillingAccount to recharge.
        credits: Number of credits to recharge.
        entity_label: Label for logging (e.g. "user abc" or "org 42").
    """
    print(
        f"[AUTO-RECHARGE] Queueing auto-recharge - {entity_label}, "
        f"BillingAccount: {billing_account.id}, Credits: {credits}, "
        f"Stripe Customer ID: {billing_account.stripe_customer_id}",
    )

    now = datetime.now(timezone.utc)
    invoice_group = month_end_utc(now)

    recharge = Recharge(
        billing_account_id=billing_account.id,
        type=RECHARGE_TYPE_AUTO,
        quantity=Decimal(credits),
        amount_usd=Decimal(credits),  # 1 credit = $1
        invoice_group=invoice_group,
        status=RechargeStatus.PENDING_INVOICE,
    )

    session.add(recharge)

    print(
        f"[AUTO-RECHARGE] Record created for billing_account {billing_account.id}: "
        f"${credits:.2f} ({credits} credits), Invoice group: {invoice_group}",
    )

    # Create Stripe invoice item if billing account has Stripe customer ID
    if billing_account.stripe_customer_id:
        print(
            f"[AUTO-RECHARGE] Creating Stripe invoice item for "
            f"billing_account {billing_account.id}",
        )

        try:
            if not settings.stripe_secret_key:
                print("[AUTO-RECHARGE] ERROR: stripe_secret_key not configured!")
                return

            stripe.api_key = settings.stripe_secret_key

            invoice_item = stripe.InvoiceItem.create(
                customer=billing_account.stripe_customer_id,
                amount=int(credits * 100),  # Convert to cents
                currency="usd",
                description=f"{credits} credits (auto-recharge)",
                metadata={
                    "recharge_type": "auto",
                    "billing_account_id": str(billing_account.id),
                    "invoice_group": str(invoice_group),
                },
            )

            print(
                f"[AUTO-RECHARGE] SUCCESS: Stripe invoice item created - "
                f"Invoice Item ID: {invoice_item.id}",
            )

        except stripe.error.StripeError as e:
            print(
                f"[AUTO-RECHARGE] STRIPE ERROR: {type(e).__name__} - {str(e)}",
            )

        except Exception as e:
            print(
                f"[AUTO-RECHARGE] UNEXPECTED ERROR: {type(e).__name__} - {str(e)}",
            )
    else:
        print(
            f"[AUTO-RECHARGE] WARNING: BillingAccount {billing_account.id} "
            f"has no Stripe customer ID",
        )
