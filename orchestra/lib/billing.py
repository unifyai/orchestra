"""Shared billing utilities for Orchestra.

All billing operations now operate through BillingAccount.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Union

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
) -> bool:
    """
    Create a Stripe InvoiceItem then record an auto-recharge.

    The InvoiceItem is created **first** so that credits are only granted
    when the billable artifact exists on Stripe.  If InvoiceItem creation
    fails (network error, invalid customer, etc.), no credits are granted
    and no recharge record is written — preventing revenue leakage.

    Returns True if the recharge was queued, False if skipped or failed.
    """
    if not billing_account.stripe_customer_id:
        logger.warning(
            "Auto-recharge skipped for billing_account %s (%s): "
            "no stripe_customer_id",
            billing_account.id,
            entity_label,
        )
        return False

    now = datetime.now(timezone.utc)
    invoice_group = month_end_utc(now)

    logger.info(
        "Queueing auto-recharge – %s, BillingAccount: %s, Credits: %s, "
        "Stripe Customer ID: %s",
        entity_label,
        billing_account.id,
        credits,
        billing_account.stripe_customer_id,
    )

    # Step 1: Create the Stripe InvoiceItem BEFORE any DB changes.
    # If this fails the function returns False and the caller's session
    # is left untouched — no free credits.
    try:
        configure_stripe()

        invoice_item = stripe.InvoiceItem.create(
            customer=billing_account.stripe_customer_id,
            amount=int(credits * 100),
            currency="usd",
            description=f"{credits} credits (auto-recharge)",
            metadata={
                "recharge_type": "auto",
                "billing_account_id": str(billing_account.id),
                "invoice_group": str(invoice_group),
            },
        )

        logger.info(
            "Stripe InvoiceItem created: %s for billing_account %s",
            invoice_item.id,
            billing_account.id,
        )

    except stripe.StripeError as e:
        logger.error(
            "Stripe error creating InvoiceItem for billing_account %s (%s): "
            "%s — credits NOT granted",
            billing_account.id,
            entity_label,
            e,
        )
        return False

    except Exception as e:
        logger.error(
            "Unexpected error creating InvoiceItem for billing_account %s (%s): "
            "%s — credits NOT granted",
            billing_account.id,
            entity_label,
            e,
        )
        return False

    # Step 2: Record the recharge in the DB and grant credits.
    # The InvoiceItem already exists on Stripe at this point.  If the DB
    # write fails we best-effort delete the InvoiceItem to avoid orphans.
    try:
        recharge = Recharge(
            billing_account_id=billing_account.id,
            type=RECHARGE_TYPE_AUTO,
            quantity=Decimal(credits),
            amount_usd=Decimal(credits),
            invoice_group=invoice_group,
            status=RechargeStatus.PENDING_INVOICE,
        )
        session.add(recharge)
        billing_account.credits = billing_account.credits + Decimal(credits)

        logger.info(
            "Auto-recharge recorded for billing_account %s: "
            "$%.2f (%s credits), group: %s, new balance: %s",
            billing_account.id,
            credits,
            credits,
            invoice_group,
            billing_account.credits,
        )
    except Exception as e:
        logger.error(
            "DB error after InvoiceItem %s created for billing_account %s: %s "
            "— attempting cleanup",
            invoice_item.id,
            billing_account.id,
            e,
        )
        try:
            stripe.InvoiceItem.delete(invoice_item.id)
            logger.info("Cleaned up orphaned InvoiceItem %s", invoice_item.id)
        except Exception as cleanup_err:
            logger.error(
                "Failed to clean up InvoiceItem %s: %s",
                invoice_item.id,
                cleanup_err,
            )
        return False

    return True


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
        configure_stripe()

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


# =========================================================================
# Tax ID display helpers
# =========================================================================

# ISO 3166-1 alpha-2 → human-readable country name.
# Used by the supported-tax-countries endpoint so frontends don't need
# their own hardcoded mapping.
COUNTRY_NAMES: Dict[str, str] = {
    "US": "United States",
    "GB": "United Kingdom",
    "AU": "Australia",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "NL": "Netherlands",
    "BE": "Belgium",
    "AT": "Austria",
    "SE": "Sweden",
    "DK": "Denmark",
    "FI": "Finland",
    "IE": "Ireland",
    "PT": "Portugal",
    "NO": "Norway",
    "CH": "Switzerland",
    "JP": "Japan",
    "KR": "South Korea",
    "IN": "India",
    "SG": "Singapore",
    "MY": "Malaysia",
    "TH": "Thailand",
    "BR": "Brazil",
    "MX": "Mexico",
    "RU": "Russia",
    "CN": "China",
    "NZ": "New Zealand",
    "ZA": "South Africa",
    "BG": "Bulgaria",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "EE": "Estonia",
    "GR": "Greece",
    "HR": "Croatia",
    "HU": "Hungary",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MT": "Malta",
    "PL": "Poland",
    "RO": "Romania",
    "SI": "Slovenia",
    "SK": "Slovakia",
}

# Tax module identifier → human-readable name and expected input format.
# Used by the supported-tax-countries endpoint so frontends receive
# structured data instead of having to parse description strings.
TAX_TYPE_MAP: Dict[str, Dict[str, str]] = {
    "us.ein": {"name": "EIN", "format": "XX-XXXXXXX"},
    "gb.vat": {"name": "VAT Number", "format": "GB999999999"},
    "au.abn": {"name": "ABN", "format": "XX XXX XXX XXX"},
    "ca.gst_hst": {"name": "GST/HST Number", "format": "XXXXXXXXX"},
    "de.vat": {"name": "VAT Number", "format": "DEXXXXXXXXX"},
    "fr.tva": {"name": "TVA Number", "format": "FRXXXXXXXXXXX"},
    "it.iva": {"name": "IVA Number", "format": "ITXXXXXXXXXXX"},
    "es.vat": {"name": "VAT Number", "format": "ESXXXXXXXXX"},
    "jp.cn": {"name": "Corporate Number", "format": "XXXXXXXXXXXXX"},
    "nl.btw": {"name": "BTW Number", "format": "NLXXXXXXXXX"},
    "be.vat": {"name": "VAT Number", "format": "BEXXXXXXXXX"},
    "at.uid": {"name": "UID Number", "format": "ATXXXXXXXXX"},
    "se.vat": {"name": "VAT Number", "format": "SEXXXXXXXXX"},
    "dk.cvr": {"name": "CVR Number", "format": "XXXXXXXX"},
    "pt.nif": {"name": "NIF Number", "format": "XXXXXXXXX"},
    "no.mva": {"name": "MVA Number", "format": "XXXXXXXXX"},
    "ch.vat": {"name": "VAT Number", "format": "CHXXXXXXXXX"},
    "kr.brn": {"name": "Business Registration Number", "format": "XXX-XX-XXXXX"},
    "in.gstin": {"name": "GSTIN", "format": "XXXXXXXXXXXX"},
    "sg.uen": {"name": "UEN", "format": "XXXXXXXXX"},
    "my.nric": {"name": "NRIC/Company No.", "format": "XXXXXXXXX"},
    "th.moa": {"name": "MOA Number", "format": "XXXXXXXXX"},
    "br.cnpj": {"name": "CNPJ", "format": "XX.XXX.XXX/XXXX-XX"},
    "mx.rfc": {"name": "RFC", "format": "XXXXXXXXXXX"},
    "ru.inn": {"name": "INN", "format": "XXXXXXXXXX"},
    "cn.uscc": {"name": "USCC", "format": "XXXXXXXXXXXXXXXXX"},
}


def extract_tax_id_info(description: str) -> Dict[str, str]:
    """Extract structured tax ID info from a description string.

    Parses descriptions produced by
    :pymethod:`TaxIDValidator.get_supported_countries` — e.g.
    ``"Full validation (us.ein)"`` or ``"EU VAT validation"`` — and
    returns ``{"name": ..., "format": ..., "tax_id_type": ...}``.
    """
    match = re.search(r"\(([^)]+)\)", description)
    if match:
        tax_type = match.group(1)
        info = TAX_TYPE_MAP.get(tax_type)
        if info:
            return {**info, "tax_id_type": tax_type}
    if "EU VAT" in description:
        return {
            "name": "VAT Number",
            "format": "Enter VAT number",
            "tax_id_type": "eu_vat",
        }
    return {"name": "Tax ID", "format": "Enter tax ID"}
