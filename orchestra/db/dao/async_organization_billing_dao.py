"""Async version of organization_billing_dao for use with AsyncSession."""

"""Async Data Access Object for organization billing operations."""
import decimal
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Organization

logger = logging.getLogger(__name__)

# Minimum auto-recharge amount for organizations ($25 to avoid tiny invoices)
MIN_ORG_AUTORECHARGE_AMOUNT = decimal.Decimal("25")


class AsyncOrganizationBillingDAO:
    """
    DAO for organization billing operations.

    Handles credit management, autorecharge settings, and business profile
    for organizations with direct billing enabled.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, organization_id: int) -> Optional[Organization]:
        """
        Get an organization by ID.

        :param organization_id: Organization ID.
        :return: Organization object or None if not found.
        """
        return (
            self.session.query(Organization)
            .filter(Organization.id == organization_id)
            .first()
        )

    async def get_by_stripe_customer_id(
        self,
        stripe_customer_id: str,
    ) -> Optional[Organization]:
        """
        Get an organization by its Stripe customer ID.

        :param stripe_customer_id: The Stripe customer ID.
        :return: Organization object or None if not found.
        """
        query = select(Organization).where(
            Organization.stripe_customer_id == stripe_customer_id,
        )
        return (await self.session.execute(query)).scalars().first()

    async def has_direct_billing(self, organization_id: int) -> bool:
        """
        Check if an organization has direct billing enabled.

        Direct billing is enabled when stripe_customer_id is set.
        Organizations without stripe_customer_id use delegated billing
        through billing_user_id.

        :param organization_id: Organization ID.
        :return: True if organization has direct billing enabled.
        """
        org = self.get(organization_id)
        if org is None:
            return False
        return org.stripe_customer_id is not None

    async def get_credits(self, organization_id: int) -> decimal.Decimal:
        """
        Get the current credit balance for an organization.

        :param organization_id: Organization ID.
        :return: Credit balance or 0 if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return decimal.Decimal("0")
        return org.credits

    async def add_credits(
        self,
        organization_id: int,
        quantity: float,
    ) -> Optional[decimal.Decimal]:
        """
        Add credits to an organization's wallet.

        :param organization_id: Organization ID.
        :param quantity: Positive number of credits to add.
        :return: New credit balance, or None if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return None

        new_credits = org.credits + decimal.Decimal(str(quantity))
        org.credits = new_credits
        return new_credits

    async def deduct_credits(
        self,
        organization_id: int,
        quantity: float,
    ) -> Optional[decimal.Decimal]:
        """
        Deduct credits from an organization's wallet.

        :param organization_id: Organization ID.
        :param quantity: Positive number of credits to deduct.
        :return: New credit balance, or None if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return None

        new_credits = org.credits - decimal.Decimal(str(quantity))

        # Warn if credits go negative (shouldn't happen with autorecharge)
        if new_credits < 0:
            logger.warning(
                f"Organization {organization_id} credits went negative: {new_credits}. "
                f"Deducted {quantity} from {org.credits}.",
            )

        org.credits = new_credits
        return new_credits

    async def set_stripe_customer_id(
        self,
        organization_id: int,
        stripe_customer_id: str,
    ) -> bool:
        """
        Set the Stripe customer ID for an organization.

        This enables direct billing mode for the organization.

        :param organization_id: Organization ID.
        :param stripe_customer_id: Stripe customer ID.
        :return: True if successful, False if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        org.stripe_customer_id = stripe_customer_id
        return True

    async def get_autorecharge_settings(
        self,
        organization_id: int,
    ) -> Optional[dict]:
        """
        Get autorecharge settings for an organization.

        :param organization_id: Organization ID.
        :return: Dict with autorecharge settings, or None if not found.
        """
        org = self.get(organization_id)
        if org is None:
            return None

        return {
            "autorecharge": org.autorecharge,
            "autorecharge_threshold": float(org.autorecharge_threshold),
            "autorecharge_qty": float(org.autorecharge_qty),
        }

    async def set_autorecharge(
        self,
        organization_id: int,
        enabled: bool,
    ) -> bool:
        """
        Enable or disable autorecharge for an organization.

        :param organization_id: Organization ID.
        :param enabled: Whether autorecharge should be enabled.
        :return: True if successful, False if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        org.autorecharge = enabled
        return True

    async def set_autorecharge_threshold(
        self,
        organization_id: int,
        threshold: float,
    ) -> bool:
        """
        Set the autorecharge threshold for an organization.

        :param organization_id: Organization ID.
        :param threshold: Credit level that triggers autorecharge.
        :return: True if successful, False if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        org.autorecharge_threshold = decimal.Decimal(str(threshold))
        return True

    async def set_autorecharge_qty(
        self,
        organization_id: int,
        qty: float,
    ) -> bool:
        """
        Set the autorecharge quantity for an organization.

        Enforces a minimum of $25 to avoid tiny monthly invoices.

        :param organization_id: Organization ID.
        :param qty: Amount of credits to add during autorecharge.
        :return: True if successful, False if organization not found.
        :raises ValueError: If qty is below the minimum threshold.
        """
        qty_decimal = decimal.Decimal(str(qty))

        if qty_decimal < MIN_ORG_AUTORECHARGE_AMOUNT:
            raise ValueError(
                f"Minimum auto-recharge amount for organizations is "
                f"${MIN_ORG_AUTORECHARGE_AMOUNT}. Got ${qty_decimal}.",
            )

        org = self.get(organization_id)
        if org is None:
            return False

        org.autorecharge_qty = qty_decimal
        return True

    async def should_trigger_autorecharge(self, organization_id: int) -> bool:
        """
        Check if an organization should trigger an autorecharge.

        Returns True if:
        - Organization exists
        - Has direct billing (stripe_customer_id set)
        - Autorecharge is enabled
        - Current credits are at or below threshold

        :param organization_id: Organization ID.
        :return: True if autorecharge should be triggered.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        # Must have direct billing enabled
        if org.stripe_customer_id is None:
            return False

        # Must have autorecharge enabled
        if not org.autorecharge:
            return False

        # Check if below threshold
        return org.credits <= org.autorecharge_threshold

    # Valid account status values
    VALID_ACCOUNT_STATUSES = {"ACTIVE", "SUSPENDED", "PAST_DUE", "CLOSED"}

    async def set_account_status(
        self,
        organization_id: int,
        status: str,
    ) -> bool:
        """
        Set the account status for an organization.

        :param organization_id: Organization ID.
        :param status: Account status. Must be one of: ACTIVE, SUSPENDED, PAST_DUE, CLOSED.
        :return: True if successful, False if organization not found.
        :raises ValueError: If status is not a valid value.
        """
        if status not in self.VALID_ACCOUNT_STATUSES:
            raise ValueError(
                f"Invalid account status: '{status}'. "
                f"Must be one of: {', '.join(sorted(self.VALID_ACCOUNT_STATUSES))}",
            )

        org = self.get(organization_id)
        if org is None:
            return False

        org.account_status = status
        return True

    async def is_account_active(self, organization_id: int) -> bool:
        """
        Check if an organization's account is active.

        :param organization_id: Organization ID.
        :return: True if account is active, False otherwise.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        return org.account_status == "ACTIVE"

    async def update_business_profile(
        self,
        organization_id: int,
        billing_email: Optional[str] = None,
        business_name: Optional[str] = None,
        tax_id: Optional[str] = None,
        billing_address: Optional[dict] = None,
    ) -> bool:
        """
        Update the business profile for an organization.

        Only updates fields that are provided (not None).

        :param organization_id: Organization ID.
        :param billing_email: Email for billing communications.
        :param business_name: Legal business name for invoices.
        :param tax_id: Tax identification number.
        :param billing_address: JSONB address dict with flexible structure.
            Supports international formats with fields like:
            - country: ISO 3166-1 alpha-2 code (e.g., "US", "IN", "GB")
            - formatted: Full formatted address for display
            - line1, line2: Street address lines
            - city, state, postal_code: Standard fields
            - locality, district: For countries that use these
        :return: True if successful, False if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        if billing_email is not None:
            org.billing_email = billing_email
        if business_name is not None:
            org.business_name = business_name
        if tax_id is not None:
            org.tax_id = tax_id
        if billing_address is not None:
            # Merge with existing address if partial update
            existing = org.billing_address or {}
            org.billing_address = {**existing, **billing_address}

        return True

    async def set_billing_address(
        self,
        organization_id: int,
        billing_address: dict,
    ) -> bool:
        """
        Set the complete billing address for an organization.

        Replaces the entire address (unlike update_business_profile which merges).

        :param organization_id: Organization ID.
        :param billing_address: Complete address dict.
        :return: True if successful, False if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        org.billing_address = billing_address
        return True

    async def get_business_profile(self, organization_id: int) -> Optional[dict]:
        """
        Get the business profile for an organization.

        :param organization_id: Organization ID.
        :return: Dict with business profile data, or None if not found.
        """
        org = self.get(organization_id)
        if org is None:
            return None

        return {
            "billing_email": org.billing_email,
            "business_name": org.business_name,
            "tax_id": org.tax_id,
            "billing_address": org.billing_address or {},
        }

    async def set_billing_setup_complete(
        self,
        organization_id: int,
        complete: bool,
    ) -> bool:
        """
        Mark whether billing setup is complete for an organization.

        :param organization_id: Organization ID.
        :param complete: Whether billing setup is complete.
        :return: True if successful, False if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        org.billing_setup_complete = complete
        return True

    async def clear_delegated_billing(self, organization_id: int) -> bool:
        """
        Clear delegated billing for an organization.

        Sets billing_user_id to None, indicating the organization
        uses its own wallet instead of a user's wallet.

        :param organization_id: Organization ID.
        :return: True if successful, False if organization not found.
        """
        org = self.get(organization_id)
        if org is None:
            return False

        org.billing_user_id = None
        return True
