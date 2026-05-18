"""Shared billing utilities for Orchestra.

This module is the home for cross-DAO billing orchestration:

* :class:`BillingEntity` and :func:`get_billing_entity` — uniform handle on
  the billing account behind a user or organization. Useful when feature
  code has a request-context (user_id / organization_id) but needs to
  resolve the billing account, autorecharge settings, etc.
* :func:`queue_auto_recharge` — Stripe-side autorecharge mechanics.
  No-ops for METERED accounts so the same low-balance trigger code
  works on both modes.
* Stripe helpers (:func:`configure_stripe`, :func:`sync_billing_profile_to_stripe`,
  :func:`ensure_stripe_customer`, etc.).

The billable-action primitives (``add_credits`` / ``deduct_credits``)
live on :class:`BillingAccountDAO` directly and dispatch on the account's
billing mode (CREDITS mutates the wallet, METERED writes a ledger-only
audit row). Feature code calls those DAO methods directly.
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
    BillingMode,
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
    billing_mode: BillingMode = BillingMode.CREDITS

    @property
    def is_user(self) -> bool:
        """Check if this is a user billing entity."""
        return self.entity_type == BillingEntityType.USER

    @property
    def is_organization(self) -> bool:
        """Check if this is an organization billing entity."""
        return self.entity_type == BillingEntityType.ORGANIZATION

    @property
    def is_metered(self) -> bool:
        """METERED accounts settle usage at month-end via the metered invoicer."""
        return self.billing_mode == BillingMode.METERED

    @property
    def has_billing(self) -> bool:
        """Check if entity has Stripe customer ID for direct billing."""
        return self.stripe_customer_id is not None

    def has_sufficient_credits(self, cost: Decimal) -> bool:
        """Pre-flight check for whether a billable action can be allowed.

        METERED accounts always pass: their usage is recorded to the
        ``CreditTransaction`` ledger and settled at month-end by
        :mod:`orchestra.routines.monthly_metered_invoicer`. The wallet
        on a METERED account is frozen (no automatic writes from
        ``deduct_credits``) and may carry any leftover balance from a
        prior CREDITS phase — neither a positive nor a negative balance
        should gate billable actions.

        CREDITS accounts must have ``credits >= cost``.
        """
        if self.is_metered:
            return True
        return self.credits >= cost

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
        if self.is_metered:
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
            billing_mode=ba_dao.resolve_billing_mode(ba),
        )

    # Organization context
    ba = ba_dao.resolve_for_org(organization_id)
    if ba is None:
        raise ValueError(
            f"Organization {organization_id} not found or has no billing "
            f"set up. Please set up billing in the organization settings.",
        )

    if ba.account_status in ("SUSPENDED", "CLOSED"):
        raise ValueError(
            f"Organization {organization_id} is {ba.account_status}. "
            f"Billing operations not allowed.",
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
        billing_mode=ba_dao.resolve_billing_mode(ba),
    )


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

    METERED-mode accounts are short-circuited at the top: they pay by
    monthly invoice (handled by ``monthly_metered_invoicer``), not by
    topping up a credits wallet. Routing this guard through the central
    ``queue_auto_recharge`` rather than every call site means the
    contact levy, low-balance triggers, and any future caller all
    benefit without per-site changes.
    """
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.models.orchestra_models import BillingMode

    if (
        BillingAccountDAO(session).resolve_billing_mode(billing_account)
        == BillingMode.METERED
    ):
        logger.debug(
            "Auto-recharge skipped for billing_account %s (%s): "
            "account is METERED — invoiced via monthly_metered_invoicer",
            billing_account.id,
            entity_label,
        )
        return False

    if not billing_account.stripe_customer_id:
        logger.warning(
            "Auto-recharge skipped for billing_account %s (%s): "
            "no stripe_customer_id",
            billing_account.id,
            entity_label,
        )
        return False

    # Verify the customer still has a valid payment method before
    # creating an InvoiceItem that will inevitably fail at collection.
    try:
        configure_stripe()
        customer = stripe.Customer.retrieve(
            billing_account.stripe_customer_id,
            expand=["invoice_settings.default_payment_method"],
        )
        has_pm = bool(
            (
                customer.invoice_settings
                and customer.invoice_settings.default_payment_method
            )
            or customer.default_source,
        )
        if not has_pm:
            logger.warning(
                {
                    "message": "Auto-recharge skipped: no payment method on file",
                    "billing_account_id": billing_account.id,
                    "stripe_customer_id": billing_account.stripe_customer_id,
                    "entity": entity_label,
                },
            )
            billing_account.autorecharge = False
            return False
    except stripe.InvalidRequestError:
        logger.warning(
            {
                "message": "Auto-recharge skipped: Stripe customer not found or deleted",
                "billing_account_id": billing_account.id,
                "stripe_customer_id": billing_account.stripe_customer_id,
                "entity": entity_label,
            },
        )
        billing_account.autorecharge = False
        return False
    except stripe.StripeError as e:
        logger.warning(
            {
                "message": "Auto-recharge skipped: Stripe API error checking payment method",
                "billing_account_id": billing_account.id,
                "error": str(e),
            },
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
        try:
            from orchestra.routines.billing_notifications import (
                notify_billing_event_failure,
            )

            notify_billing_event_failure(
                "auto_recharge",
                error=str(e),
                context_id=f"ba_{billing_account.id}",
                billing_account_id=billing_account.id,
            )
        except Exception:
            logger.warning("Failed to send billing event notification", exc_info=True)
        return False

    except Exception as e:
        logger.error(
            "Unexpected error creating InvoiceItem for billing_account %s (%s): "
            "%s — credits NOT granted",
            billing_account.id,
            entity_label,
            e,
        )
        try:
            from orchestra.routines.billing_notifications import (
                notify_billing_event_failure,
            )

            notify_billing_event_failure(
                "auto_recharge",
                error=str(e),
                context_id=f"ba_{billing_account.id}",
                billing_account_id=billing_account.id,
            )
        except Exception:
            logger.warning("Failed to send billing event notification", exc_info=True)
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
        ba_dao = BillingAccountDAO(session)
        new_balance = ba_dao.add_credits(
            billing_account.id,
            float(credits),
            category="recharge",
            description=f"Auto-recharge ({credits} credits)",
            detail={"event": "auto_recharge", "invoice_group": str(invoice_group)},
        )

        logger.info(
            "Auto-recharge recorded for billing_account %s: "
            "$%.2f (%s credits), group: %s, new balance: %s",
            billing_account.id,
            credits,
            credits,
            invoice_group,
            new_balance,
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
        try:
            from orchestra.routines.billing_notifications import (
                notify_billing_event_failure,
            )

            notify_billing_event_failure(
                "auto_recharge_db",
                error=str(e),
                context_id=f"ba_{billing_account.id}",
                billing_account_id=billing_account.id,
            )
        except Exception:
            logger.warning("Failed to send billing event notification", exc_info=True)
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


# =========================================================================
# Stripe Customer provisioning
# =========================================================================
#
# METERED-mode invoicing requires ``BillingAccount.stripe_customer_id`` to
# be populated so the metered invoicer can attach a ``stripe.Invoice`` and
# (for SEND_INVOICE_NET_30) email the hosted invoice page. Today's CREDITS
# accounts get a Stripe Customer automatically when the user first goes
# through Checkout (``customer_creation=always``); enterprise accounts that
# were funded via admin-granted credits and never hit Checkout never had one
# created. ``ensure_stripe_customer`` plugs that hole — it's safe to call
# from the admin plan-assignment endpoint and as a defensive backstop in
# the metered invoicer itself.


def ensure_stripe_customer(
    session: Session,
    billing_account: BillingAccount,
    *,
    is_business: Optional[bool] = None,
    fallback_name: Optional[str] = None,
    fallback_email: Optional[str] = None,
) -> str:
    """Return ``stripe_customer_id``, creating the Stripe Customer if absent.

    Idempotent. If ``billing_account.stripe_customer_id`` is already set,
    returns it without touching Stripe. Otherwise creates a Stripe
    Customer using whatever profile fields are populated on the
    ``BillingAccount`` (billing email/address/tax_id) and falling back to
    the ``fallback_*`` parameters for fields that are missing.

    Args:
        session: Database session — used to persist the new
            ``stripe_customer_id`` back onto the account.
        billing_account: The account that needs a Stripe Customer.
        is_business: Hint for ``build_stripe_customer_name`` so that org
            accounts get ``name=`` set to the business name (and personal
            accounts to the user's display name). Inferred from
            ``billing_account.business_name`` if omitted.
        fallback_name: Display/business name to use when neither
            ``billing_account.name`` nor ``business_name`` is set. Useful
            for orgs whose name lives on the ``Organization`` row, not on
            ``BillingAccount``.
        fallback_email: Email to use when ``billing_account.billing_email``
            is unset (commonly the user's account email).

    Returns:
        The ``stripe_customer_id`` (existing or freshly created).

    Raises:
        stripe.error.StripeError: If the API call fails.
        RuntimeError: If neither billing_email nor fallback_email is
            available — Stripe requires an email to send invoices.
    """
    from orchestra.web.api.utils.business_validation import build_stripe_customer_name

    if billing_account.stripe_customer_id:
        return billing_account.stripe_customer_id

    configure_stripe()

    email = billing_account.billing_email or fallback_email
    if not email:
        raise RuntimeError(
            f"Cannot create Stripe Customer for billing_account "
            f"{billing_account.id}: no billing_email on file and no "
            "fallback_email provided. Ask the customer to set their "
            "billing email on the Billing Profile, or pass "
            "fallback_email when calling.",
        )

    # Default to personal-account treatment unless the caller hints otherwise
    # (the admin endpoint passes is_business=True for org-backed accounts).
    if is_business is None:
        is_business = False

    create_params: Dict[str, Any] = {"email": email}

    name_value = billing_account.name or fallback_name
    if name_value:
        create_params.update(
            build_stripe_customer_name(is_business=is_business, name=name_value),
        )

    address = billing_account.billing_address or {}
    if address.get("line1"):
        create_params["address"] = {
            "line1": address.get("line1", ""),
            "line2": address.get("line2", ""),
            "city": address.get("city", ""),
            "state": address.get("state", ""),
            "postal_code": address.get("postal_code", ""),
            "country": address.get("country", ""),
        }

    # Stamp our internal id into Stripe metadata so the customer is
    # reverse-traceable from the Stripe dashboard.
    create_params["metadata"] = {
        "orchestra_billing_account_id": str(billing_account.id),
    }

    customer = stripe.Customer.create(**create_params)
    customer_id = customer["id"]

    billing_account.stripe_customer_id = customer_id
    session.flush()

    # Attach tax id if we already have one (best-effort; failure here
    # shouldn't block customer creation). ``sync_tax_id_to_stripe``
    # already swallows Stripe-side errors internally, so anything we
    # see here is either a config lookup miss
    # (``get_stripe_tax_id_type`` raising on an unknown country) or a
    # genuinely unexpected import/runtime error — log loudly and
    # carry on rather than masking it with bare ``Exception``.
    if billing_account.tax_id:
        try:
            from orchestra.web.api.utils.business_validation import (
                sync_tax_id_to_stripe,
            )

            country_code = address.get("country") if address else None
            sync_tax_id_to_stripe(
                customer_id,
                billing_account.tax_id,
                country_code,
                logger=logger,
            )
        except stripe.error.StripeError as exc:
            logger.warning(
                "Created Stripe Customer %s but Stripe rejected tax_id "
                "attach (customer is usable, tax_id is not synced): %s",
                customer_id,
                exc,
            )
        except (KeyError, ValueError) as exc:
            # Unknown country code or unmapped tax_id_type — surfaces
            # the mismatch loudly so we can extend the lookup tables
            # rather than silently shipping customers without a tax id.
            logger.error(
                "Created Stripe Customer %s but tax_id mapping is "
                "incomplete (country=%r, tax_id=%r): %s — extend "
                "TAX_TYPE_MAP / get_stripe_tax_id_type",
                customer_id,
                address.get("country") if address else None,
                billing_account.tax_id,
                exc,
                exc_info=True,
            )

    logger.info(
        "Created Stripe Customer %s for billing_account %s",
        customer_id,
        billing_account.id,
    )
    return customer_id


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
