"""Shared billing utilities for Orchestra.

All billing operations now operate through BillingAccount.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Union

import stripe
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    BillingAccount,
    Recharge,
    RechargeStatus,
)
from orchestra.lib.time import month_end_utc
from orchestra.settings import settings

logger = logging.getLogger(__name__)


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
    ba_dao = BillingAccountDAO(session)

    if organization_id is None:
        # Personal context – bill the user directly
        ba = ba_dao.resolve_for_user(user_id)
        if ba is None:
            raise ValueError(
                f"User {user_id} not found or has no billing account.",
            )
        return BillingEntity(
            entity_type=BillingEntityType.USER,
            entity_id=user_id,
            billing_account_id=ba.id,
            credits=ba.credits,
            stripe_customer_id=ba.stripe_customer_id,
            autorecharge=ba.autorecharge,
            autorecharge_threshold=ba.autorecharge_threshold,
            autorecharge_qty=ba.autorecharge_qty,
        )

    # Organization context
    ba = ba_dao.resolve_for_org(organization_id)
    if ba is None:
        raise ValueError(
            f"Organization {organization_id} not found or has no billing "
            f"set up. Please set up billing in the organization settings.",
        )

    if ba.account_status != "ACTIVE":
        raise ValueError(
            f"Organization {organization_id} is {ba.account_status}. "
            f"Billing operations not allowed for non-active organizations.",
        )

    return BillingEntity(
        entity_type=BillingEntityType.ORGANIZATION,
        entity_id=organization_id,
        billing_account_id=ba.id,
        credits=ba.credits,
        stripe_customer_id=ba.stripe_customer_id,
        autorecharge=ba.autorecharge,
        autorecharge_threshold=ba.autorecharge_threshold,
        autorecharge_qty=ba.autorecharge_qty,
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
    ba_dao = BillingAccountDAO(session)
    new_balance = ba_dao.deduct_credits(
        billing_entity.billing_account_id,
        float(amount),
    )
    if new_balance is None:
        raise ValueError(
            f"BillingAccount {billing_entity.billing_account_id} not found.",
        )
    return new_balance


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

    # Credit the billing account immediately (pay-now, invoice-later model).
    # The monthly invoicer will bill the customer at month-end; if the
    # invoice payment fails, the account will be marked PAST_DUE.
    billing_account.credits = billing_account.credits + Decimal(credits)

    print(
        f"[AUTO-RECHARGE] Record created for billing_account {billing_account.id}: "
        f"${credits:.2f} ({credits} credits), Invoice group: {invoice_group}. "
        f"Credits added immediately (new balance: {billing_account.credits})",
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


# =========================================================================
# Stripe helpers
# =========================================================================


def configure_stripe() -> None:
    """
    Configure the ``stripe`` module with the secret key from settings.

    Raises ``RuntimeError`` when the key is not configured so callers can
    translate this into an appropriate HTTP error.
    """
    if not settings.stripe_secret_key:
        raise RuntimeError("Stripe is not configured on this server.")
    stripe.api_key = settings.stripe_secret_key


def is_stripe_mode_conflict(error: Exception) -> bool:
    """
    Detect whether a Stripe API error is caused by a live-mode / test-mode
    key mismatch — e.g. a live-mode customer ID being used with a test-mode
    secret key (or vice versa).
    """
    msg = str(getattr(error, "user_message", "")) or str(error)
    return "live mode" in msg and "test mode" in msg


def prefill_customer_fields(
    customer_id: str,
    email: Optional[str],
    name: Optional[str],
) -> None:
    """
    Best-effort update of email/name on an existing Stripe customer so that
    Checkout pre-fills those fields.

    - **email**: Always synced to the canonical value from our DB.
    - **name**: Only set when missing on the customer record (we don't
      overwrite a name the customer may have entered themselves).
    """
    try:
        customer = stripe.Customer.retrieve(customer_id)
        if getattr(customer, "deleted", False):
            return

        update: dict = {}
        if email and customer.email != email:
            update["email"] = email
        if name and not customer.name:
            update["name"] = name

        if update:
            stripe.Customer.modify(customer_id, **update)
    except Exception:
        logger.warning(
            "Failed to pre-fill Stripe customer fields (non-fatal) %s",
            customer_id,
            exc_info=True,
        )


def sync_tax_id_to_customer(
    customer_id: str,
    tax_id: str,
    tax_id_type: Optional[str] = None,
) -> None:
    """
    Ensure a tax ID is present on a Stripe customer (idempotent).

    Only adds the tax ID if no entry with the same ``value`` already
    exists on the customer.
    """
    try:
        existing = stripe.Customer.list_tax_ids(customer_id)
        already_exists = any(t.value == tax_id for t in existing.data)
        if not already_exists:
            stripe.Customer.create_tax_id(
                customer_id,
                type=tax_id_type or "eu_vat",
                value=tax_id,
            )
    except Exception:
        logger.warning(
            "Failed to sync tax ID for Stripe customer %s",
            customer_id,
            exc_info=True,
        )


# =========================================================================
# Billing Profile → Stripe sync
# =========================================================================


def sync_billing_profile_to_stripe(
    stripe_customer_id: str,
    *,
    is_business: bool,
    billing_email: Optional[str] = None,
    name: Optional[str] = None,
    tax_id: Optional[str] = None,
    billing_address: Optional[dict] = None,
    existing_billing_address: Optional[dict] = None,
    logger_instance: Any = None,
) -> None:
    """
    Sync billing profile fields to an existing Stripe customer.

    This is the shared implementation used by both user and organization
    billing-profile update endpoints.  It is best-effort: failures are
    logged but do **not** propagate.

    Args:
        stripe_customer_id: Stripe customer ID.
        is_business: True for organization accounts, False for personal.
        billing_email: Updated email for invoices (None = skip).
        name: Updated display name (None = skip).
        tax_id: Updated tax ID value (None = skip sync).
        billing_address: New address dict from the update request.
        existing_billing_address: The billing address already stored on the
            BillingAccount (used as fallback for country when resolving
            tax ID type).
        logger_instance: Optional logger; falls back to module logger.
    """
    from orchestra.web.api.utils.business_validation import (
        build_stripe_customer_name,
        sync_tax_id_to_stripe,
    )

    log = logger_instance or logger

    try:
        if not settings.stripe_secret_key:
            return
        stripe.api_key = settings.stripe_secret_key

        update_params: dict = {}

        if billing_email is not None:
            update_params["email"] = billing_email

        if name is not None:
            update_params.update(
                build_stripe_customer_name(
                    is_business=is_business,
                    name=name,
                ),
            )

        if billing_address and billing_address.get("line1"):
            update_params["address"] = {
                "line1": billing_address.get("line1", ""),
                "line2": billing_address.get("line2", ""),
                "city": billing_address.get("city", ""),
                "state": billing_address.get("state", ""),
                "postal_code": billing_address.get("postal_code", ""),
                "country": billing_address.get("country", ""),
            }
            update_params["tax"] = {"validate_location": "immediately"}

        if update_params:
            stripe.Customer.modify(stripe_customer_id, **update_params)

        # Sync tax ID (requires separate Stripe API calls)
        if tax_id is not None:
            country_code = None
            if billing_address and billing_address.get("country"):
                country_code = billing_address["country"]
            elif existing_billing_address and existing_billing_address.get(
                "country",
            ):
                country_code = existing_billing_address["country"]

            sync_tax_id_to_stripe(
                stripe_customer_id,
                tax_id,
                country_code,
                logger=log,
            )

    except Exception as e:
        log.warning(
            "Failed to sync billing profile to Stripe for %s: %s",
            stripe_customer_id,
            e,
        )
