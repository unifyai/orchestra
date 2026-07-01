"""Shared billing utilities for Orchestra.

This module is the home for cross-DAO billing orchestration:

* :class:`BillingEntity` and :func:`get_billing_entity` — uniform handle on
  the billing account behind a user or organization. Useful when feature
  code has a request-context (user_id / organization_id) but needs to
  resolve the billing account, billing mode, etc.
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
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Union

import stripe
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.orchestra_models import BillingAccount, BillingMode
from orchestra.services.personal_workspace_service import personal_workspace_is_disabled
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
        if personal_workspace_is_disabled(session, user_id):
            raise ValueError(
                "Personal workspace is disabled for organization members.",
            )

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
        billing_mode=ba_dao.resolve_billing_mode(ba),
    )


# =========================================================================
# No-charge credit grants
# =========================================================================


def grant_promo_credits(
    session: Session,
    billing_account: BillingAccount,
    amount: float,
    *,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    description: str = "Promo credit",
    detail: Optional[Dict[str, Any]] = None,
    invoice_group: Optional[Any] = None,
) -> Optional[Decimal]:
    """Grant free (promotional) credits with no Stripe charge.

    Credits the CREDITS wallet via :meth:`BillingAccountDAO.add_credits`
    (which also appends the ledger row) and records a ``PAID`` ``promo``
    recharge so the top-up shows in history and a later depletion is treated
    as a paid depletion by the out-of-credits banner. Returns the new wallet
    balance (``None`` for METERED accounts, where the wallet is frozen).

    Shared by the admin recharge endpoint (``type="promo"``) and the
    self-serve manual top-up endpoint (staging) — neither involves Stripe.
    """
    from datetime import datetime, timezone

    from orchestra.db.dao.recharge_dao import RechargeDAO
    from orchestra.db.models.orchestra_models import RechargeStatus
    from orchestra.lib.time import month_end_utc

    ba_dao = BillingAccountDAO(session)
    new_balance = ba_dao.add_credits(
        billing_account.id,
        float(amount),
        category="promo",
        user_id=user_id,
        organization_id=organization_id,
        description=description,
        detail=detail or {"event": "promo_recharge"},
    )

    RechargeDAO(session).create_recharge(
        billing_account_id=billing_account.id,
        quantity=int(amount),
        amount_usd=Decimal(str(amount)),
        invoice_group=invoice_group or month_end_utc(datetime.now(timezone.utc)),
        type_="promo",
        status=RechargeStatus.PAID,
    )
    return new_balance


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


def validate_address_tax_location(address: Optional[dict]) -> Optional[str]:
    """Check that Stripe Tax can resolve a jurisdiction for ``address``.

    Stripe is the source of truth for whether an address maps to a real tax
    location (a US address needs a valid ZIP/state; many countries resolve
    from the country alone). We probe it with a *throwaway* customer created
    with ``tax.validate_location='immediately'`` — Stripe raises when the
    location can't be resolved — then delete the probe so nothing is
    persisted on the real account.

    This lets the billing-profile save fail fast with a clear message instead
    of the customer only discovering the problem at checkout (where the
    Subscription create is what trips Stripe Tax).

    Returns a user-facing error string when the address is rejected, or
    ``None`` when it's accepted *or* when the check can't run (missing
    country, Stripe not configured, transient API error) — we never block a
    profile save on a probe failure that isn't a definitive location rejection.
    """
    if not address or not (address.get("country") or "").strip():
        return None

    probe = None
    try:
        configure_stripe()
        probe = stripe.Customer.create(
            address={
                "line1": address.get("line1", ""),
                "line2": address.get("line2", ""),
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "postal_code": address.get("postal_code", ""),
                "country": address.get("country", ""),
            },
            tax={"validate_location": "immediately"},
        )
    except stripe.error.InvalidRequestError as exc:
        msg = str(exc).lower()
        if "location" in msg or "address" in msg or "tax" in msg:
            return (
                "We couldn't verify this billing address for tax. Please check "
                "it's a complete, valid address — including a correct postal/ZIP "
                "code and, for US addresses, a state."
            )
        # Some other invalid-request reason (not an address-location problem) —
        # don't block the save on it.
        return None
    except Exception:  # noqa: BLE001 — never block a save on a probe failure.
        return None

    # Accepted: clean up the throwaway probe customer (best-effort).
    try:
        stripe.Customer.delete(probe["id"])
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_stripe_customer(
    session: Session,
    billing_account: BillingAccount,
    *,
    is_business: Optional[bool] = None,
    name: Optional[str] = None,
    email: Optional[str] = None,
    address: Optional[dict] = None,
    tax_id: Optional[str] = None,
    fallback_name: Optional[str] = None,
    fallback_email: Optional[str] = None,
) -> str:
    """Return ``stripe_customer_id``, creating the Stripe Customer if absent.

    Idempotent. If ``billing_account.stripe_customer_id`` is already set,
    returns it without touching Stripe.

    Billing profile data is **not** read from the ``BillingAccount`` (PII is
    no longer stored locally — Stripe is the source of truth). Callers that
    have profile data on hand (the billing-profile save path) pass it in via
    ``name`` / ``email`` / ``address`` / ``tax_id``; callers that don't (the
    admin/invoicer/setup-intent backstops) create a bare Customer with just
    the fallback email + metadata, and the profile is synced later when the
    customer saves it.

    Args:
        session: Database session — used to persist the new
            ``stripe_customer_id`` back onto the account.
        billing_account: The account that needs a Stripe Customer.
        is_business: Hint for ``build_stripe_customer_name`` so business
            accounts get ``business_name`` set (personal → ``individual_name``).
            Defaults to personal treatment.
        name: Display/business name to seed the Customer with.
        email: Invoice email to seed the Customer with.
        address: Billing address dict to seed the Customer with.
        tax_id: Tax ID to attach to the new Customer.
        fallback_name: Name to use when ``name`` is not supplied.
        fallback_email: Email to use when ``email`` is not supplied
            (commonly the user's account email).

    Returns:
        The ``stripe_customer_id`` (existing or freshly created).

    Raises:
        stripe.error.StripeError: If the API call fails.
        RuntimeError: If neither ``email`` nor ``fallback_email`` is
            available — Stripe requires an email to send invoices.
    """
    from orchestra.web.api.utils.business_validation import build_stripe_customer_name

    if billing_account.stripe_customer_id:
        return billing_account.stripe_customer_id

    configure_stripe()

    resolved_email = email or fallback_email
    if not resolved_email:
        raise RuntimeError(
            f"Cannot create Stripe Customer for billing_account "
            f"{billing_account.id}: no email provided. Ask the customer to "
            "set their billing email on the Billing Profile, or pass "
            "fallback_email when calling.",
        )

    # Default to personal-account treatment unless the caller hints otherwise
    # (the admin endpoint passes is_business=True for org-backed accounts).
    if is_business is None:
        is_business = False

    create_params: Dict[str, Any] = {"email": resolved_email}

    name_value = name or fallback_name
    if name_value:
        create_params.update(
            build_stripe_customer_name(is_business=is_business, name=name_value),
        )

    address = address or {}
    # Sync the address whenever a country is present (the minimum Stripe Tax
    # needs to resolve a location), not just when a street line exists —
    # self-serve subscribers are gated on country, which must reach Stripe.
    if address.get("country") or address.get("line1"):
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

    # Attach tax id if one was supplied (best-effort; failure here shouldn't
    # block customer creation). ``sync_tax_id_to_stripe`` already swallows
    # Stripe-side errors internally, so anything we see here is either a
    # config lookup miss (``get_stripe_tax_id_type`` raising on an unknown
    # country) or a genuinely unexpected import/runtime error — log loudly
    # and carry on rather than masking it with bare ``Exception``.
    if tax_id:
        try:
            from orchestra.web.api.utils.business_validation import (
                sync_tax_id_to_stripe,
            )

            country_code = address.get("country") if address else None
            sync_tax_id_to_stripe(
                customer_id,
                tax_id,
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
                tax_id,
                exc,
                exc_info=True,
            )

    logger.info(
        "Created Stripe Customer %s for billing_account %s",
        customer_id,
        billing_account.id,
    )
    return customer_id


def fetch_billing_profile_from_stripe(
    stripe_customer_id: Optional[str],
) -> Dict[str, Any]:
    """Read the editable billing profile back from the Stripe Customer.

    Stripe is the source of truth for billing PII (name / email / address /
    tax ID). This returns the dict shape the billing-profile response builders
    expect (the same shape the local DAO used to return before billing PII was
    moved to Stripe), so response builders don't have to change.

    Best-effort: returns empty/None values if there's no customer yet or if
    Stripe is unreachable, so the profile screen degrades gracefully rather
    than erroring.
    """
    empty: Dict[str, Any] = {
        "billing_email": None,
        "name": None,
        "tax_id": None,
        "tax_id_type": None,
        "tax_id_verification_status": None,
        "billing_address": {},
    }
    if not stripe_customer_id:
        return empty

    try:
        configure_stripe()
        customer = stripe.Customer.retrieve(stripe_customer_id)
    except stripe.error.StripeError as exc:
        logger.warning(
            "Could not fetch billing profile from Stripe for %s: %s",
            stripe_customer_id,
            exc,
        )
        return empty

    address = customer.get("address") or {}
    profile: Dict[str, Any] = {
        "billing_email": customer.get("email"),
        "name": customer.get("name"),
        "tax_id": None,
        "tax_id_type": None,
        "tax_id_verification_status": None,
        "billing_address": (
            {
                "line1": address.get("line1") or "",
                "line2": address.get("line2") or "",
                "city": address.get("city") or "",
                "state": address.get("state") or "",
                "postal_code": address.get("postal_code") or "",
                "country": address.get("country") or "",
            }
            if address
            else {}
        ),
    }

    try:
        tax_ids = stripe.Customer.list_tax_ids(stripe_customer_id, limit=1)
        if tax_ids and tax_ids.data:
            first = tax_ids.data[0]
            profile["tax_id"] = first.get("value")
            profile["tax_id_type"] = first.get("type")
            verification = first.get("verification") or {}
            profile["tax_id_verification_status"] = verification.get("status")
    except stripe.error.StripeError as exc:
        logger.warning(
            "Could not fetch tax IDs from Stripe for %s: %s",
            stripe_customer_id,
            exc,
        )

    return profile


#: Fields that must all be present for an address to resolve a tax
#: jurisdiction for Stripe automatic tax (a country alone covers country-level
#: VAT, but sub-national tax — e.g. US sales tax — needs postal_code + the
#: city/line so Stripe can place the customer).
_REQUIRED_ADDRESS_FIELDS = ("line1", "city", "postal_code", "country")


def is_billing_address_complete(address: Optional[Dict[str, Any]]) -> bool:
    """Return whether ``address`` is complete enough for Stripe automatic tax.

    This is the single definition behind the derived ``billing_setup_complete``
    flag: callers recompute the flag from the *live* Stripe address whenever it
    changes (profile PATCH, admin edit, ``customer.updated`` webhook) so the
    flag never drifts from Stripe — without storing the address (PII) locally.
    """
    if not address:
        return False
    return all((address.get(f) or "").strip() for f in _REQUIRED_ADDRESS_FIELDS)


class PaymentMethodError(Exception):
    """Raised for invalid payment-method management requests."""


def create_setup_intent(customer_id: str) -> str:
    """Create a Stripe SetupIntent for saving a card, return its client secret.

    ``usage="off_session"`` so the saved card can be charged for future
    subscription renewals without the customer present. The client secret is
    handed to Stripe Elements in the browser to confirm the card; the secret
    key never leaves the backend.
    """
    configure_stripe()
    intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=["card"],
        usage="off_session",
    )
    return intent["client_secret"]


def list_payment_methods(customer_id: str) -> list[dict]:
    """List the customer's saved cards, flagging the default for invoices.

    Returns a list of plain dicts (id, brand, last4, exp month/year, and
    ``is_default``) — the customer's ``invoice_settings.default_payment_method``
    is the card that backs subscription renewals.
    """
    configure_stripe()
    customer = stripe.Customer.retrieve(customer_id)
    invoice_settings = (
        customer.get("invoice_settings")
        if isinstance(customer, dict)
        else getattr(customer, "invoice_settings", None)
    ) or {}
    default_pm = (
        invoice_settings.get("default_payment_method")
        if isinstance(invoice_settings, dict)
        else getattr(invoice_settings, "default_payment_method", None)
    )

    methods = stripe.PaymentMethod.list(customer=customer_id, type="card")
    data = methods.get("data", []) if isinstance(methods, dict) else methods.data
    cards: list[dict] = []
    for pm in data or []:
        pm_id = pm.get("id") if isinstance(pm, dict) else pm.id
        card = (
            pm.get("card") if isinstance(pm, dict) else getattr(pm, "card", None)
        ) or {}
        get = (
            card.get
            if isinstance(card, dict)
            else (lambda k, c=card: getattr(c, k, None))
        )
        cards.append(
            {
                "id": pm_id,
                "brand": get("brand"),
                "last4": get("last4"),
                "exp_month": get("exp_month"),
                "exp_year": get("exp_year"),
                "is_default": pm_id == default_pm,
            },
        )
    return cards


def resolve_default_payment_method(customer_id: str) -> Optional[str]:
    """Return the payment method to charge a self-serve subscription on.

    Prefers the customer's invoice default (``invoice_settings.default_
    payment_method``). When none is explicitly set but the customer has
    saved cards (e.g. they added one in the in-app manager without
    promoting it), falls back to the most recently attached card so a
    first subscribe can still charge off-session. Returns ``None`` when the
    customer has no card on file at all.

    Used by ``create_subscription`` to charge the first invoice off-session
    instead of redirecting to a Stripe-hosted page.
    """
    configure_stripe()
    customer = stripe.Customer.retrieve(customer_id)
    invoice_settings = (
        customer.get("invoice_settings")
        if isinstance(customer, dict)
        else getattr(customer, "invoice_settings", None)
    ) or {}
    default_pm = (
        invoice_settings.get("default_payment_method")
        if isinstance(invoice_settings, dict)
        else getattr(invoice_settings, "default_payment_method", None)
    )
    if default_pm:
        return (
            default_pm
            if isinstance(default_pm, str)
            else getattr(default_pm, "id", None)
        )

    methods = stripe.PaymentMethod.list(customer=customer_id, type="card")
    data = methods.get("data", []) if isinstance(methods, dict) else methods.data
    for pm in data or []:
        pm_id = pm.get("id") if isinstance(pm, dict) else pm.id
        if pm_id:
            return pm_id
    return None


def set_default_payment_method(
    customer_id: str,
    subscription_id: Optional[str],
    payment_method_id: str,
) -> None:
    """Make ``payment_method_id`` the default card for future invoices.

    Sets it on the customer's ``invoice_settings`` (used for new invoices)
    and, when there's an active subscription, on the subscription itself so
    renewals charge the chosen card.
    """
    configure_stripe()
    stripe.Customer.modify(
        customer_id,
        invoice_settings={"default_payment_method": payment_method_id},
    )
    if subscription_id:
        stripe.Subscription.modify(
            subscription_id,
            default_payment_method=payment_method_id,
        )


def detach_payment_method(payment_method_id: str) -> None:
    """Detach (remove) a saved card from its customer."""
    configure_stripe()
    stripe.PaymentMethod.detach(payment_method_id)


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
