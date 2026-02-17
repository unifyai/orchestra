"""Data Access Object for BillingAccount operations."""

import decimal
import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    BillingAccount,
    Recharge,
    RechargeStatus,
)

logger = logging.getLogger(__name__)

# Minimum auto-recharge amount ($25 to avoid tiny invoices)
MIN_AUTORECHARGE_AMOUNT = decimal.Decimal("25")

# Minimum cumulative spending (in USD) required before a billing account
# can enable auto-recharge.  This is a fraud-prevention measure to stop
# bot accounts from setting up very low, repeated automatic top-ups and
# then disputing the charges.
MIN_SPEND_FOR_AUTO_RECHARGE = decimal.Decimal("100")

# Valid account status values
VALID_ACCOUNT_STATUSES = {"ACTIVE", "PAST_DUE", "SUSPENDED", "CLOSED"}


class BillingAccountDAO:
    """
    DAO for BillingAccount operations.

    Provides a unified interface for all billing operations that works
    identically for User and Organization billing accounts.
    """

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # CRUD
    # =========================================================================

    def create(self, **kwargs) -> BillingAccount:
        """
        Create a new billing account.

        :param kwargs: Optional initial field values (credits, tier, etc.)
        :return: The created BillingAccount instance.
        """
        billing_account = BillingAccount(**kwargs)
        self.session.add(billing_account)
        self.session.flush()  # Get the ID
        return billing_account

    def get(self, billing_account_id: int) -> Optional[BillingAccount]:
        """
        Get a billing account by ID.

        :param billing_account_id: BillingAccount ID.
        :return: BillingAccount object or None.
        """
        return (
            self.session.query(BillingAccount)
            .filter(BillingAccount.id == billing_account_id)
            .first()
        )

    def get_by_stripe_customer_id(
        self,
        stripe_customer_id: str,
    ) -> Optional[BillingAccount]:
        """
        Get a billing account by its Stripe customer ID.

        :param stripe_customer_id: The Stripe customer ID.
        :return: BillingAccount object or None.
        """
        query = select(BillingAccount).where(
            BillingAccount.stripe_customer_id == stripe_customer_id,
        )
        return self.session.execute(query).scalars().first()

    # =========================================================================
    # CREDITS
    # =========================================================================

    def get_credits(self, billing_account_id: int) -> decimal.Decimal:
        """
        Get the current credit balance.

        :param billing_account_id: BillingAccount ID.
        :return: Credit balance or 0.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return decimal.Decimal("0")
        return ba.credits

    def add_credits(
        self,
        billing_account_id: int,
        quantity: float,
    ) -> Optional[decimal.Decimal]:
        """
        Add credits to a billing account.

        :param billing_account_id: BillingAccount ID.
        :param quantity: Positive number of credits to add.
        :return: New credit balance, or None if not found.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return None

        new_credits = ba.credits + decimal.Decimal(str(quantity))
        ba.credits = new_credits
        return new_credits

    def deduct_credits(
        self,
        billing_account_id: int,
        quantity: float,
    ) -> Optional[decimal.Decimal]:
        """
        Deduct credits from a billing account.

        :param billing_account_id: BillingAccount ID.
        :param quantity: Positive number of credits to deduct.
        :return: New credit balance, or None if not found.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return None

        new_credits = ba.credits - decimal.Decimal(str(quantity))

        if new_credits < 0:
            logger.warning(
                f"BillingAccount {billing_account_id} credits went negative: "
                f"{new_credits}. Deducted {quantity} from {ba.credits}.",
            )

        ba.credits = new_credits
        return new_credits

    # =========================================================================
    # STRIPE
    # =========================================================================

    def set_stripe_customer_id(
        self,
        billing_account_id: int,
        stripe_customer_id: str,
    ) -> bool:
        """
        Set the Stripe customer ID for a billing account.

        :param billing_account_id: BillingAccount ID.
        :param stripe_customer_id: Stripe customer ID.
        :return: True if successful, False if not found.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.stripe_customer_id = stripe_customer_id
        return True

    def has_billing(self, billing_account_id: int) -> bool:
        """
        Check if a billing account has direct billing enabled.

        :param billing_account_id: BillingAccount ID.
        :return: True if stripe_customer_id is set.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        return ba.stripe_customer_id is not None

    # =========================================================================
    # AUTORECHARGE
    # =========================================================================

    def get_autorecharge_settings(
        self,
        billing_account_id: int,
    ) -> Optional[dict]:
        """
        Get autorecharge settings.

        :param billing_account_id: BillingAccount ID.
        :return: Dict with autorecharge settings, or None.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return None

        return {
            "autorecharge": ba.autorecharge,
            "autorecharge_threshold": float(ba.autorecharge_threshold),
            "autorecharge_qty": float(ba.autorecharge_qty),
        }

    def set_autorecharge(
        self,
        billing_account_id: int,
        enabled: bool,
    ) -> bool:
        """Enable or disable autorecharge."""
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.autorecharge = enabled
        return True

    def set_autorecharge_threshold(
        self,
        billing_account_id: int,
        threshold: float,
    ) -> bool:
        """Set the autorecharge threshold."""
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.autorecharge_threshold = decimal.Decimal(str(threshold))
        return True

    def set_autorecharge_qty(
        self,
        billing_account_id: int,
        qty: float,
    ) -> bool:
        """
        Set the autorecharge quantity.

        :raises ValueError: If qty is below minimum.
        """
        qty_decimal = decimal.Decimal(str(qty))
        if qty_decimal < MIN_AUTORECHARGE_AMOUNT:
            raise ValueError(
                f"Minimum auto-recharge amount is "
                f"${MIN_AUTORECHARGE_AMOUNT}. Got ${qty_decimal}.",
            )

        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.autorecharge_qty = qty_decimal
        return True

    def should_trigger_autorecharge(self, billing_account_id: int) -> bool:
        """
        Check if autorecharge should be triggered.

        Returns True if billing account has direct billing, autorecharge enabled,
        and credits at or below threshold.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        if ba.stripe_customer_id is None:
            return False
        if not ba.autorecharge:
            return False
        return ba.credits <= ba.autorecharge_threshold

    # =========================================================================
    # ACCOUNT STATUS
    # =========================================================================

    def set_account_status(
        self,
        billing_account_id: int,
        status: str,
    ) -> bool:
        """
        Set the account status.

        :param billing_account_id: BillingAccount ID.
        :param status: Must be ACTIVE, PAST_DUE, SUSPENDED, or CLOSED.
        :raises ValueError: If status is invalid.
        """
        if status not in VALID_ACCOUNT_STATUSES:
            raise ValueError(
                f"Invalid account status: '{status}'. "
                f"Must be one of: {', '.join(sorted(VALID_ACCOUNT_STATUSES))}",
            )

        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.account_status = status
        return True

    def is_account_active(self, billing_account_id: int) -> bool:
        """Check if account is active."""
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        return ba.account_status == "ACTIVE"

    def is_account_frozen_or_suspended(self, billing_account_id: int) -> bool:
        """Check if account is frozen (SUSPENDED or CLOSED)."""
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        return ba.account_status in ("SUSPENDED", "CLOSED")

    # =========================================================================
    # AUTO-RECHARGE ELIGIBILITY (fraud prevention)
    # =========================================================================

    def get_total_spending(self, billing_account_id: int) -> decimal.Decimal:
        """
        Calculate the cumulative amount (USD) a billing account has spent.

        Only considers recharges with status PAID and types 'payment' and
        'auto' (i.e. real money transactions – not promos).

        :param billing_account_id: BillingAccount ID.
        :return: Total spending in USD.
        """
        result = (
            self.session.query(func.coalesce(func.sum(Recharge.amount_usd), 0))
            .filter(
                Recharge.billing_account_id == billing_account_id,
                Recharge.status == RechargeStatus.PAID,
                Recharge.type.in_(["payment", "auto", "invoice"]),
            )
            .scalar()
        )
        return decimal.Decimal(str(result))

    def can_enable_auto_recharge(self, billing_account_id: int) -> bool:
        """
        Check whether a billing account is eligible to enable auto-recharge.

        The account must have spent at least ``MIN_SPEND_FOR_AUTO_RECHARGE``
        in real-money transactions.  This prevents bot accounts from setting
        up very low, repeated automatic top-ups and then disputing the
        charges.

        :param billing_account_id: BillingAccount ID.
        :return: True if cumulative spending meets the threshold.
        """
        total = self.get_total_spending(billing_account_id)
        return total >= MIN_SPEND_FOR_AUTO_RECHARGE

    # =========================================================================
    # BILLING SETUP
    # =========================================================================

    def set_billing_setup_complete(
        self,
        billing_account_id: int,
        complete: bool,
    ) -> bool:
        """Mark whether billing setup is complete."""
        ba = self.get(billing_account_id)
        if ba is None:
            return False
        ba.billing_setup_complete = complete
        return True

    # =========================================================================
    # BILLING PROFILE
    # =========================================================================

    def update_billing_profile(
        self,
        billing_account_id: int,
        billing_email: Optional[str] = None,
        name: Optional[str] = None,
        tax_id: Optional[str] = None,
        tax_id_type: Optional[str] = None,
        billing_address: Optional[dict] = None,
    ) -> bool:
        """
        Update the business profile.

        Only updates fields that are provided (not None).
        Works identically for personal users and organizations.

        :param billing_account_id: BillingAccount ID.
        :param billing_email: Email for invoices.
        :param name: Display name (individual or business).
        :param tax_id: Tax identification number.
        :param tax_id_type: Stripe tax ID type code.
        :param billing_address: JSONB address dict.
        :return: True if successful, False if not found.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return False

        if billing_email is not None:
            ba.billing_email = billing_email
        if name is not None:
            ba.name = name
        if tax_id is not None:
            ba.tax_id = tax_id
        if tax_id_type is not None:
            ba.tax_id_type = tax_id_type
        if billing_address is not None:
            # Merge with existing address if partial update
            existing = ba.billing_address or {}
            ba.billing_address = {**existing, **billing_address}

        return True

    def get_billing_profile(self, billing_account_id: int) -> Optional[dict]:
        """
        Get the billing profile.

        :param billing_account_id: BillingAccount ID.
        :return: Dict with billing profile data, or None.
            The ``name`` key is entity-agnostic; callers should map it
            to ``individual_name`` or ``business_name`` as appropriate.
        """
        ba = self.get(billing_account_id)
        if ba is None:
            return None

        return {
            "billing_email": ba.billing_email,
            "name": ba.name,
            "tax_id": ba.tax_id,
            "tax_id_type": ba.tax_id_type,
            "billing_address": ba.billing_address or {},
            "billing_setup_complete": ba.billing_setup_complete,
        }
