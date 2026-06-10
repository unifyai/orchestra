"""
Billing API endpoints – Stripe checkout, portal, status, billing profiles,
tax validation, and organization billing management.

These endpoints replace direct Stripe SDK calls that were previously made by
the console frontend.  The frontend now calls these thin wrappers instead,
keeping the Stripe secret key exclusively on the backend.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.param_functions import Depends
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.lib.billing import (
    COUNTRY_NAMES,
    PaymentMethodError,
    configure_stripe,
    create_setup_intent,
    detach_payment_method,
    extract_tax_id_info,
    is_stripe_mode_conflict,
    list_payment_methods,
    set_default_payment_method,
    sync_billing_profile_to_stripe,
)
from orchestra.web.api.billing.schema import (
    AccountInfoResponse,
    AutoIncrementResponse,
    AutoIncrementUpdateRequest,
    AvailablePlanItem,
    AvailablePlansResponse,
    BillingProfileResponse,
    BillingProfileUpdate,
    CancelSubscriptionResponse,
    CurrentPeriodUsageResponse,
    CurrentPlanSummary,
    InvoiceListItem,
    InvoiceListResponse,
    InvoiceUrlsResponse,
    PaymentMethodListResponse,
    PortalSessionResponse,
    SetupIntentResponse,
    SubscribeRequest,
    SubscribeResponse,
    SwitchPlanRequest,
    SwitchPlanResponse,
    TaxIdValidationRequest,
)
from orchestra.web.api.utils.business_validation import get_stripe_tax_id_type
from orchestra.web.api.utils.tax_id_validator import (
    TaxIDValidator,
    validate_tax_id_for_country,
)

router = APIRouter()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_stripe() -> None:
    """Configure Stripe API key or raise an HTTP 500."""
    try:
        configure_stripe()
    except RuntimeError as exc:
        logger.error(f"Stripe configuration failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Stripe is not configured")


def _check_org_billing_permission(
    session,
    user_id: str,
    organization_id: Optional[int],
    permission: str,
) -> None:
    """
    Enforce ``billing:read`` or ``billing:write`` for org-context requests.

    Personal-context requests (``organization_id is None``) are always
    allowed — the user is managing their own billing account.

    :raises HTTPException: 403 when the user lacks the required permission.
    """
    if organization_id is None:
        return  # Personal context — always allowed

    ra_dao = ResourceAccessDAO(session)
    if not ra_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        permission,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"You do not have {permission} permission " f"in this organization"
            ),
        )


# ============================================================================
# GET /billing/account-info  (user-facing)
# ============================================================================


@router.get(
    "/billing/account-info",
    response_model=AccountInfoResponse,
    responses={
        200: {"description": "Billing account information"},
        400: {"description": "Billing not set up"},
    },
)
def get_account_info(
    request_fastapi: Request,
    session=Depends(get_db_session),
) -> AccountInfoResponse:
    """
    Return billing account information for the authenticated user / org.

    The response includes credit balance, Stripe customer status,
    account status, and current plan/subscription details.  Context
    (personal vs org) is derived from the API key.
    """
    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )

    _check_org_billing_permission(
        session,
        user_id,
        organization_id,
        "billing:read",
    )

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    # Find the most recent paid recharge for this billing account
    recharge_dao = RechargeDAO(session)
    last_recharge = recharge_dao.get_last_paid(ba.id)
    last_recharge_at: Optional[str] = None
    if last_recharge and last_recharge.at:
        ts = last_recharge.at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        last_recharge_at = ts.isoformat()

    # Resolve the effective plan via the DAO (every account has an
    # active assignment from signup — the default plan — so the
    # call always returns a real plan) and project Decimals onto the
    # JSON-friendly response schema. ``billing_mode`` is also surfaced
    # at the top level so the frontend can branch without drilling
    # into the nested ``plan`` object.
    from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO

    plan = BillingPlanAssignmentDAO(session).resolve_effective_plan(ba.id)
    plan_summary = CurrentPlanSummary.from_effective_plan(plan)

    # Self-serve subscription fields for the console subscription page.
    # ``is_subscribed`` requires BOTH an active Stripe subscription id and a
    # self-serve tier plan (CREDITS / STRIPE_SUBSCRIPTION) — that pairing is
    # the only state where the subscription-billing UI applies.
    from orchestra.db.models.enums import BillingMode, CollectionMethod

    is_subscribed = bool(ba.stripe_subscription_id) and (
        str(plan.billing_mode) == BillingMode.CREDITS
        and str(plan.collection_method) == CollectionMethod.STRIPE_SUBSCRIPTION
    )

    next_renewal_at: Optional[str] = None
    if is_subscribed and ba.current_period_end is not None:
        renewal = ba.current_period_end
        if renewal.tzinfo is None:
            renewal = renewal.replace(tzinfo=timezone.utc)
        next_renewal_at = renewal.isoformat()

    # Surface the unconsumed signup *trial* grant's expiry from the
    # expiring-grant ledger — but only for not-yet-subscribed accounts (a
    # subscriber has no live trial grant to show).
    trial_expires_at: Optional[str] = None
    if not is_subscribed:
        from orchestra.lib.credit_grants import GRANT_KIND_TRIAL, compute_grant_lots

        trial_lots = [
            lot
            for lot in compute_grant_lots(session, ba.id)
            if lot.grant_kind == GRANT_KIND_TRIAL and lot.remaining > 0
        ]
        if trial_lots:
            # Soonest-expiring live trial lot (there is normally just one).
            soonest = min(trial_lots, key=lambda lot: lot.expires_at)
            trial_expires_at = soonest.expires_at.isoformat()

    return AccountInfoResponse(
        billing_account_id=ba.id,
        credits=float(ba.credits) if ba.credits else 0.0,
        account_status=ba.account_status or "ACTIVE",
        last_recharge_at=last_recharge_at,
        billing_mode=plan.billing_mode,
        plan=plan_summary,
        plan_group_id=ba.plan_group_id,
        is_subscribed=is_subscribed,
        trial_expires_at=trial_expires_at,
        next_renewal_at=next_renewal_at,
        subscription_cancel_at_period_end=(
            bool(ba.subscription_cancel_at_period_end) if is_subscribed else False
        ),
    )


# ============================================================================
# POST /billing/portal-session
# ============================================================================


# Cached id of the restricted billing-portal configuration (created lazily,
# once per process). The portal is now only used by METERED accounts for
# invoice history — CREDITS manage cards + billing profile in-app — so the
# config hides the duplicate payment-method and customer-info editors.
_RESTRICTED_PORTAL_CONFIG_ID: Optional[str] = None


def _get_restricted_portal_configuration_id() -> Optional[str]:
    """Return (creating + caching once) a stripped-down portal configuration.

    Returns ``None`` if the configuration can't be created (e.g. the Stripe
    account has no default business profile set), so the caller falls back to
    the account-default portal rather than failing the request.
    """
    global _RESTRICTED_PORTAL_CONFIG_ID
    if _RESTRICTED_PORTAL_CONFIG_ID:
        return _RESTRICTED_PORTAL_CONFIG_ID
    try:
        config = stripe.billing_portal.Configuration.create(
            features={
                # The reason a METERED customer opens the portal.
                "invoice_history": {"enabled": True},
                # Cards + billing details are now managed in-app; hide the
                # duplicate editors.
                "payment_method_update": {"enabled": False},
                "customer_update": {"enabled": False},
            },
            metadata={"orchestra_portal": "metered_invoice_history_only"},
        )
        _RESTRICTED_PORTAL_CONFIG_ID = config.id
        return config.id
    except Exception:
        logger.warning(
            "Could not create restricted billing-portal configuration; "
            "falling back to the account-default portal.",
            exc_info=True,
        )
        return None


@router.post(
    "/billing/portal-session",
    response_model=PortalSessionResponse,
    include_in_schema=False,
    responses={
        200: {"description": "Portal session created"},
        404: {"description": "No Stripe customer found"},
        500: {"description": "Stripe configuration error"},
    },
)
def create_portal_session(
    request_fastapi: Request,
    session=Depends(get_db_session),
) -> PortalSessionResponse:
    """
    Create a Stripe Customer Portal session for the authenticated user / org.

    Returns the portal URL.  The caller must have previously completed a
    checkout (i.e. a Stripe customer must exist on the billing account).
    """
    _init_stripe()

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )

    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)
    if not ba or not ba.stripe_customer_id:
        raise HTTPException(
            status_code=404,
            detail=(
                "No Stripe customer ID found. "
                "Please purchase credits first to set up billing."
            ),
        )

    customer_id = ba.stripe_customer_id

    # The portal now only backs METERED accounts (CREDITS manage cards +
    # billing profile in-app), so strip the duplicate payment-method and
    # billing-info editors and leave just the invoice history. Falls back to
    # the account-default portal if the restricted config can't be built.
    portal_kwargs: dict = {"customer": customer_id}
    restricted_config_id = _get_restricted_portal_configuration_id()
    if restricted_config_id:
        portal_kwargs["configuration"] = restricted_config_id

    try:
        portal_session = stripe.billing_portal.Session.create(**portal_kwargs)
    except stripe.InvalidRequestError as exc:
        if is_stripe_mode_conflict(exc):
            logger.warning(
                "Customer %s belongs to a different Stripe mode; cannot create portal",
                customer_id,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Your billing profile was created in a different environment. "
                    "Please purchase credits first to set up billing in this environment."
                ),
            )
        logger.error(f"Stripe portal session creation failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail="Failed to create billing portal session",
        )

    return PortalSessionResponse(url=portal_session.url)


# ============================================================================
# Payment methods (in-app card management — replaces the Stripe Portal)
#
# Cards are collected client-side with Stripe Elements against a SetupIntent
# (the card never touches our servers — PCI SAQ-A), then managed here:
# list, set-default (the card that backs subscription renewals), and detach.
# ============================================================================


def _resolve_billing_customer(
    session: Session,
    request_fastapi: Request,
    permission: str,
    *,
    create_if_missing: bool = False,
):
    """Resolve the billing account + Stripe customer id, or raise 4xx.

    Shared preamble for the payment-method endpoints: enforces the billing
    permission and resolves the Stripe customer. The customer is normally
    created when the billing profile is saved; ``create_if_missing`` lets a
    write entrypoint (adding the first card) create it on demand so a card
    can be saved even before the profile sync ran. Read paths still 404 when
    there's no customer — there's nothing to list yet.
    """
    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, permission)

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    if not ba.stripe_customer_id and create_if_missing:
        from orchestra.lib.billing import ensure_stripe_customer
        from orchestra.lib.subscription_billing import resolve_is_business

        user = UserDAO(session).get_user_with_id(user_id)
        try:
            ensure_stripe_customer(
                session,
                ba,
                is_business=resolve_is_business(ba, organization_id),
                fallback_email=user.email if user else None,
                fallback_name=user.name if user else None,
            )
            session.flush()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if not ba.stripe_customer_id:
        raise HTTPException(
            status_code=404,
            detail=(
                "No billing customer yet. Save your billing profile to set "
                "up payment methods."
            ),
        )
    return ba


@router.post(
    "/billing/payment-methods/setup-intent",
    response_model=SetupIntentResponse,
    summary="Start adding a card (Stripe SetupIntent)",
    include_in_schema=False,
)
def create_payment_method_setup_intent(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> SetupIntentResponse:
    """Create a SetupIntent and return its client secret for Stripe Elements."""
    _init_stripe()
    ba = _resolve_billing_customer(
        session,
        request_fastapi,
        "billing:write",
        create_if_missing=True,
    )
    try:
        client_secret = create_setup_intent(ba.stripe_customer_id)
    except stripe.error.StripeError as exc:
        logger.error(f"Stripe error creating setup intent: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Couldn't start adding a card. Please try again.",
        )
    # Persist the Stripe customer id if it was just created on demand.
    session.commit()
    return SetupIntentResponse(client_secret=client_secret)


@router.get(
    "/billing/payment-methods",
    response_model=PaymentMethodListResponse,
    summary="List saved cards",
    include_in_schema=False,
)
def get_payment_methods(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> PaymentMethodListResponse:
    """List the customer's saved cards, flagging the renewal default."""
    _init_stripe()
    ba = _resolve_billing_customer(session, request_fastapi, "billing:read")
    try:
        cards = list_payment_methods(ba.stripe_customer_id)
    except stripe.error.StripeError as exc:
        logger.error(f"Stripe error listing payment methods: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Couldn't load payment methods. Please try again.",
        )
    return PaymentMethodListResponse(payment_methods=cards)


@router.post(
    "/billing/payment-methods/{payment_method_id}/default",
    response_model=PaymentMethodListResponse,
    summary="Set the default card for renewals",
    include_in_schema=False,
)
def set_payment_method_default(
    payment_method_id: str,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> PaymentMethodListResponse:
    """Make a card the default for invoices + the active subscription."""
    _init_stripe()
    ba = _resolve_billing_customer(session, request_fastapi, "billing:write")
    try:
        set_default_payment_method(
            ba.stripe_customer_id,
            ba.stripe_subscription_id,
            payment_method_id,
        )
        cards = list_payment_methods(ba.stripe_customer_id)
    except stripe.error.InvalidRequestError as exc:
        # e.g. the card isn't attached to this customer.
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.StripeError as exc:
        logger.error(f"Stripe error setting default card: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Couldn't update your default card. Please try again.",
        )
    return PaymentMethodListResponse(payment_methods=cards)


@router.delete(
    "/billing/payment-methods/{payment_method_id}",
    response_model=PaymentMethodListResponse,
    summary="Remove a saved card",
    include_in_schema=False,
)
def remove_payment_method(
    payment_method_id: str,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> PaymentMethodListResponse:
    """Detach a saved card.

    Guard: you can't remove the default card while a subscription is active —
    renewals would have nothing to charge. Set another card as default first
    (which, when only one card exists, means adding one). This keeps the
    common footgun out of the self-serve flow.
    """
    _init_stripe()
    ba = _resolve_billing_customer(session, request_fastapi, "billing:write")
    try:
        cards = list_payment_methods(ba.stripe_customer_id)
        target = next((c for c in cards if c["id"] == payment_method_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Card not found.")
        if ba.stripe_subscription_id and target["is_default"]:
            raise PaymentMethodError(
                "This is your default card for an active subscription. Set "
                "another card as default before removing it.",
            )
        detach_payment_method(payment_method_id)
        cards = list_payment_methods(ba.stripe_customer_id)
    except PaymentMethodError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.InvalidRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.StripeError as exc:
        logger.error(f"Stripe error detaching card: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Couldn't remove the card. Please try again.",
        )
    return PaymentMethodListResponse(payment_methods=cards)


# ============================================================================
# GET / PUT  /billing/auto-increment  (self-serve auto-upgrade-on-depletion)
# ============================================================================


def _auto_increment_state(session, ba) -> AutoIncrementResponse:
    """Build the auto-increment response (flag + UI-gating context)."""
    from orchestra.lib.subscription_billing import next_tier_template

    is_subscribed = bool(ba.stripe_subscription_id)
    at_top_tier = False
    if is_subscribed:
        at_top_tier = next_tier_template(session, ba) is None
    return AutoIncrementResponse(
        enabled=bool(ba.auto_increment),
        is_subscribed=is_subscribed,
        at_top_tier=at_top_tier,
    )


@router.get(
    "/billing/auto-increment",
    response_model=AutoIncrementResponse,
    summary="Read the self-serve auto-upgrade-on-depletion setting",
    description=(
        "Return whether auto-increment is enabled, plus context the "
        "console uses to gate the toggle: ``is_subscribed`` (the toggle "
        "is only meaningful on a self-serve subscription tier) and "
        "``at_top_tier`` (auto-increment hard-stops at the top of the "
        "ladder)."
    ),
)
def get_auto_increment(
    request_fastapi: Request,
    session=Depends(get_db_session),
) -> AutoIncrementResponse:
    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")
    return _auto_increment_state(session, ba)


@router.put(
    "/billing/auto-increment",
    response_model=AutoIncrementResponse,
    summary="Toggle self-serve auto-upgrade-on-depletion",
    description=(
        "Enable or disable auto-increment. When enabled, hitting a zero "
        "wallet balance auto-upgrades the subscription to the next tier "
        "up the ladder (capped at the top tier; never auto-downgrades). "
        "When disabled, depletion is a hard stop until the customer "
        "upgrades manually."
    ),
)
def update_auto_increment(
    request_fastapi: Request,
    body: AutoIncrementUpdateRequest,
    session=Depends(get_db_session),
) -> AutoIncrementResponse:
    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    ba_dao.set_auto_increment(ba.id, bool(body.enabled))
    session.commit()
    session.refresh(ba)
    return _auto_increment_state(session, ba)


# ============================================================================
# Tax Validation Endpoints (moved from users/views.py)
# ============================================================================


@router.post("/billing/validate-tax-id", include_in_schema=False)
def validate_tax_id(
    request: Request,
    body: TaxIdValidationRequest,
    session: Session = Depends(get_db_session),
):
    """Validate a tax ID format for a specific country."""
    try:
        tax_id = body.tax_id
        country = body.country
        validation_result = validate_tax_id_for_country(tax_id, country)

        return {
            "tax_id": tax_id,
            "country": country.upper(),
            "is_valid": validation_result["is_valid"],
            "formatted_tax_id": validation_result["formatted_tax_id"],
            "error": validation_result["error"],
            "supported_countries": TaxIDValidator.get_supported_countries(),
        }

    except Exception as e:
        logger.error(f"Tax ID validation failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="Tax ID validation failed")


@router.get("/billing/supported-tax-countries", include_in_schema=False)
def get_supported_tax_countries():
    """
    Get list of countries supported for tax ID validation.

    Returns structured data per country including the human-readable tax ID
    name and expected format, so frontends don't need to parse description
    strings.
    """
    raw = TaxIDValidator.get_supported_countries()
    structured: dict = {}
    for code, description in raw.items():
        info = extract_tax_id_info(description)
        structured[code] = {
            "description": description,
            "tax_id_name": info["name"],
            "tax_id_format": info["format"],
            "name": COUNTRY_NAMES.get(code, code),
            "tax_id_type": info.get("tax_id_type"),
            "stripe_tax_id_type": get_stripe_tax_id_type(code),
        }
    return {
        "supported_countries": structured,
        "total_countries": len(structured),
    }


# ============================================================================
# GET / PATCH  /billing/billing-profile
# ============================================================================


@router.get(
    "/billing/billing-profile",
    response_model=BillingProfileResponse,
    summary="Get billing profile",
    description=(
        "Get the billing profile for the current workspace. "
        "Context (personal vs org) is derived from the API key."
    ),
)
def get_billing_profile(
    request: Request,
    session: Session = Depends(get_db_session),
) -> BillingProfileResponse:
    """Return the billing profile for the API-key's billing context."""
    user_id: str = request.state.user_id
    organization_id: Optional[int] = getattr(
        request.state,
        "organization_id",
        None,
    )

    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    from orchestra.lib.subscription_billing import resolve_is_business

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)

    if not ba:
        # No billing account yet ⇒ no tax ID ⇒ treated as individual.
        # Business treatment is now keyed off the billing profile's tax ID
        # (see resolve_is_business), not org membership.
        return BillingProfileResponse(is_business=False)

    is_business = resolve_is_business(ba, organization_id)

    # PII is no longer stored locally — read the editable profile back from
    # the Stripe Customer (source of truth). The derived flags stay local.
    from orchestra.lib.billing import fetch_billing_profile_from_stripe

    profile = fetch_billing_profile_from_stripe(ba.stripe_customer_id)

    return BillingProfileResponse(
        billing_email=profile.get("billing_email"),
        name=profile.get("name"),
        tax_id=profile.get("tax_id"),
        tax_id_type=profile.get("tax_id_type"),
        billing_address=profile.get("billing_address", {}),
        billing_setup_complete=bool(ba.billing_setup_complete),
        is_business=is_business,
    )


@router.patch(
    "/billing/billing-profile",
    response_model=BillingProfileResponse,
    summary="Update billing profile",
    description=(
        "Update the billing profile for the current workspace. "
        "Context (personal vs org) is derived from the API key."
    ),
)
def update_billing_profile(
    request: Request,
    profile_update: BillingProfileUpdate,
    session: Session = Depends(get_db_session),
) -> BillingProfileResponse:
    """Update the billing profile for the API-key's billing context."""
    user_id: str = request.state.user_id
    organization_id: Optional[int] = getattr(
        request.state,
        "organization_id",
        None,
    )

    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)

    if not ba:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Billing account not found",
        )

    billing_email = profile_update.billing_email
    resolved_name = profile_update.name
    tax_id = profile_update.tax_id
    billing_address = (
        profile_update.billing_address.model_dump(exclude_unset=True)
        if profile_update.billing_address is not None
        else None
    )

    # Validate billing address if provided
    if billing_address is not None:
        from orchestra.web.api.utils.business_validation import (
            validate_billing_address_data,
        )

        addr = billing_address
        if addr.get("line1") or addr.get("city") or addr.get("country"):
            is_valid, error_msg = validate_billing_address_data(
                line1=addr.get("line1"),
                city=addr.get("city"),
                country=addr.get("country"),
                line2=addr.get("line2"),
                state=addr.get("state"),
                postal_code=addr.get("postal_code"),
            )
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid billing address: {error_msg}",
                )

            # Authoritative tax-location check: once the address is complete,
            # confirm Stripe Tax can actually resolve a jurisdiction for it
            # (e.g. a US ZIP/state that maps to a real location). This fails
            # the save with a clear message rather than letting the customer
            # hit the error later at checkout. Best-effort: skipped silently
            # if Stripe is unreachable/unconfigured (see the helper).
            address_complete = all(
                (addr.get(f) or "").strip()
                for f in ("line1", "city", "postal_code", "country")
            )
            if address_complete:
                from orchestra.lib.billing import validate_address_tax_location

                location_error = validate_address_tax_location(addr)
                if location_error:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=location_error,
                    )

    from orchestra.lib.billing import (
        ensure_stripe_customer,
        fetch_billing_profile_from_stripe,
    )

    # PII is no longer stored locally — Stripe is the source of truth. Read
    # the current profile back from Stripe so partial updates (e.g. a tax_id
    # edit without re-sending the address) can resolve the country and so the
    # response reflects the merged result.
    existing_profile = fetch_billing_profile_from_stripe(ba.stripe_customer_id)
    existing_billing_address = existing_profile.get("billing_address") or {}

    # Merge a partial address over what's already on the Stripe customer —
    # Stripe's ``Customer.modify`` replaces the whole address object, so we
    # must send the full merged dict to avoid dropping previously-saved
    # fields on a partial update.
    if billing_address is not None:
        billing_address = {**existing_billing_address, **billing_address}

    # Validate tax_id if provided along with country
    if tax_id is not None:
        country = None
        if billing_address and billing_address.get("country"):
            country = billing_address["country"]
        elif existing_billing_address.get("country"):
            country = existing_billing_address["country"]

        if country and (tax_id or "").strip():
            is_valid, formatted_id, error = TaxIDValidator.validate_tax_id(
                tax_id,
                country,
            )
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid tax ID for {country}: {error}",
                )
            tax_id = formatted_id

    # Derive business-ness from the *updated* profile: a non-empty tax ID
    # flips the account to business treatment (the customer.tax_id webhook
    # later refines this, flipping it off if Stripe rejects the ID). When the
    # request doesn't touch the tax ID, preserve the existing flag.
    if tax_id is not None:
        is_business = bool((tax_id or "").strip())
    else:
        is_business = bool(ba.is_business)

    # Create the Stripe Customer up front (at profile save) rather than
    # lazily at first payment, so the in-app payment manager can attach a
    # card *before* subscribing. Best-effort: a Stripe hiccup here must not
    # fail the profile save (the customer is also ensured at subscribe /
    # add-card time as a fallback).
    if not ba.stripe_customer_id:
        user = UserDAO(session).get_user_with_id(user_id)
        try:
            ensure_stripe_customer(
                session,
                ba,
                is_business=is_business,
                name=resolved_name,
                email=billing_email,
                address=billing_address,
                tax_id=tax_id,
                fallback_email=billing_email or (user.email if user else None),
                fallback_name=resolved_name or (user.name if user else None),
            )
        except Exception:
            logger.warning(
                "Could not create Stripe customer on profile save "
                "(will retry at subscribe/add-card)",
                exc_info=True,
            )

    # Sync the saved profile onto the Stripe customer (now that one exists).
    if ba.stripe_customer_id:
        sync_billing_profile_to_stripe(
            ba.stripe_customer_id,
            is_business=is_business,
            billing_email=billing_email,
            name=resolved_name,
            tax_id=tax_id,
            billing_address=billing_address,
            existing_billing_address=existing_billing_address,
            logger_instance=logger,
        )

    # Re-read the merged profile from Stripe so the response and the
    # ``billing_setup_complete`` gate reflect what's actually on file.
    from orchestra.lib.billing import is_billing_address_complete

    profile = fetch_billing_profile_from_stripe(ba.stripe_customer_id)
    merged_address = profile.get("billing_address") or {}
    billing_setup_complete = is_billing_address_complete(merged_address)

    ba_dao.set_billing_flags(
        billing_account_id=ba.id,
        is_business=is_business,
        billing_setup_complete=billing_setup_complete,
    )
    session.commit()

    return BillingProfileResponse(
        billing_email=profile.get("billing_email"),
        name=profile.get("name"),
        tax_id=profile.get("tax_id"),
        tax_id_type=profile.get("tax_id_type"),
        billing_address=merged_address,
        billing_setup_complete=billing_setup_complete,
        is_business=is_business,
    )


# ===========================================================================
# GET /v0/billing/invoices
#
# Customer-facing invoice list. Independent of Stripe portal access so a
# workspace member with billing:read can see what was billed even if they
# don't have access to the Stripe Dashboard. Returns INVOICE_CREATED /
# PAID / FAILED / DISPUTED rows that have an associated Stripe invoice;
# PENDING_INVOICE is internal plumbing and excluded, and rows without a
# ``stripe_invoice_id`` (manual top-ups via the admin ``payment``/``promo``
# recharge types, which credit the wallet without issuing an invoice) are
# also excluded — there's no PDF / hosted-URL to surface so they'd render
# as actionless "—" rows. Wallet credits from those flows are visible
# in the credits-balance card and any future transaction-history surface.
# ===========================================================================


@router.get(
    "/billing/invoices",
    response_model=InvoiceListResponse,
    summary="List billing invoices",
    description=(
        "Return historical invoices (and pending invoice records) for the "
        "workspace's billing account. Newest first. Includes plan version "
        "metadata for METERED-mode invoices so the audit detail (raw "
        "usage, commit, overage) is visible alongside each charge."
    ),
)
def list_billing_invoices(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_db_session),
) -> InvoiceListResponse:
    """List historical invoices for the API key's billing account."""
    from sqlalchemy import select

    from orchestra.db.models.orchestra_models import (
        BillingPlanAssignment,
        BillingPlanTemplate,
        Recharge,
        RechargeStatus,
    )

    user_id: str = request.state.user_id
    organization_id: Optional[int] = getattr(
        request.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=400,
            detail="limit must be between 1 and 200",
        )
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    # Surface every status except the internal-plumbing PENDING_INVOICE
    # bucket. Customers don't need to see "we'll invoice this at month
    # end" rows; once the invoice is created they see INVOICE_CREATED.
    visible = [
        RechargeStatus.INVOICE_CREATED,
        RechargeStatus.PAID,
        RechargeStatus.FAILED,
        RechargeStatus.DISPUTED,
    ]

    # ``stripe_invoice_id IS NOT NULL`` filters out admin-driven wallet
    # credits (``payment``/``promo`` recharges) that never produce a
    # Stripe invoice. Including them yields rows we have no PDF / hosted
    # URL for — confusing in a list framed as "Invoices". Forward-
    # compatible with any future no-invoice recharge type.
    rows = list(
        session.execute(
            select(Recharge, BillingPlanAssignment, BillingPlanTemplate)
            .outerjoin(
                BillingPlanAssignment,
                BillingPlanAssignment.id == Recharge.plan_id,
            )
            .outerjoin(
                BillingPlanTemplate,
                BillingPlanTemplate.id == BillingPlanAssignment.template_id,
            )
            .where(
                Recharge.billing_account_id == ba.id,
                Recharge.status.in_(visible),
                Recharge.stripe_invoice_id.is_not(None),
            )
            .order_by(Recharge.at.desc())
            .offset(offset)
            .limit(limit),
        ).all(),
    )

    items: list[InvoiceListItem] = []
    for recharge, _assignment, template in rows:
        ts = recharge.at
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        items.append(
            InvoiceListItem(
                id=recharge.id,
                at=ts.isoformat() if ts else "",
                type=recharge.type,
                amount_usd=float(recharge.amount_usd),
                quantity=float(recharge.quantity),
                status=recharge.status,
                invoice_group=(
                    recharge.invoice_group.isoformat()
                    if recharge.invoice_group
                    else None
                ),
                stripe_invoice_id=recharge.stripe_invoice_id,
                plan_assignment_id=recharge.plan_id,
                plan_template_name=template.name if template else None,
                plan_template_display_name=(
                    (template.display_name or template.name) if template else None
                ),
                detail=recharge.detail,
            ),
        )

    return InvoiceListResponse(
        billing_account_id=ba.id,
        invoices=items,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/billing/invoices/{recharge_id}/urls",
    response_model=InvoiceUrlsResponse,
    summary="Get Stripe-hosted view + PDF URLs for one invoice",
    description=(
        "Resolve the Stripe-hosted invoice URL and PDF URL for a single "
        "Recharge row, so the frontend can offer proper customer-facing "
        "View / Download buttons. The Recharge must belong to the "
        "caller's billing account (404 otherwise) and have a "
        "``stripe_invoice_id`` (404 if Stripe never finalised it). "
        "Returned URLs are short-lived links from Stripe; the frontend "
        "should fetch them on click rather than caching."
    ),
)
def get_invoice_urls(
    request: Request,
    recharge_id: int,
    session: Session = Depends(get_db_session),
) -> InvoiceUrlsResponse:
    from orchestra.db.models.orchestra_models import Recharge

    user_id: str = request.state.user_id
    organization_id: Optional[int] = getattr(
        request.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    recharge = session.get(Recharge, recharge_id)
    if recharge is None or recharge.billing_account_id != ba.id:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if not recharge.stripe_invoice_id:
        # Pending-invoice rows never reach Stripe; the monthly invoicer
        # rolls them up into a single per-month Stripe Invoice.
        raise HTTPException(
            status_code=404,
            detail="This invoice is not yet finalised in Stripe",
        )

    _init_stripe()
    try:
        invoice = stripe.Invoice.retrieve(recharge.stripe_invoice_id)
    except stripe.error.StripeError as exc:
        logger.warning(
            "Stripe invoice retrieve failed (recharge=%s, stripe_invoice=%s): %s",
            recharge_id,
            recharge.stripe_invoice_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail="Could not fetch invoice URLs from Stripe",
        ) from exc

    return InvoiceUrlsResponse(
        recharge_id=recharge.id,
        stripe_invoice_id=recharge.stripe_invoice_id,
        hosted_invoice_url=getattr(invoice, "hosted_invoice_url", None),
        invoice_pdf_url=getattr(invoice, "invoice_pdf", None),
    )


@router.get(
    "/billing/current-period-usage",
    response_model=CurrentPeriodUsageResponse,
    summary="Mid-period invoice estimate for the current month",
    description=(
        "Return a snapshot of where the in-progress METERED billing "
        "period stands: raw usage so far, the contract-currency "
        "equivalent, the commit floor (if any), the projected invoice "
        "line, and any commit overage. Drives the progress bar on the "
        "customer billing page. Returns 404 when the active plan is "
        "not METERED — CREDITS-mode accounts use the credits balance "
        "view instead."
    ),
)
def get_current_period_usage(
    request: Request,
    session: Session = Depends(get_db_session),
) -> CurrentPeriodUsageResponse:
    from orchestra.routines.monthly_metered_invoicer import estimate_in_progress_invoice

    user_id: str = request.state.user_id
    organization_id: Optional[int] = getattr(
        request.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    estimate = estimate_in_progress_invoice(
        session,
        billing_account_id=ba.id,
    )
    if estimate is None:
        raise HTTPException(
            status_code=404,
            detail="Active plan is not METERED",
        )

    return CurrentPeriodUsageResponse(
        period_start=estimate.period_start.date().isoformat(),
        period_end=estimate.period_end_exclusive.date().isoformat(),
        currency=estimate.currency,
        raw_usage_local=float(estimate.raw_usage_local),
        contract_usage_local=float(estimate.contract_usage_local),
        commit_amount=(
            float(estimate.commit_amount)
            if estimate.commit_amount is not None
            else None
        ),
        invoiced_estimate_local=float(estimate.invoiced_estimate_local),
        overage_local=float(estimate.overage_local),
    )


# ============================================================================
# Self-serve plan switching (plan groups)
# ============================================================================


def _classify_switch(
    *,
    is_current: bool,
    current_position: Optional[int],
    target_position: Optional[int],
) -> str:
    """Server-derived label used by both list + switch endpoints.

    Single source of truth so the UI label and the deferral rule stay
    in lock-step. ``"current"`` shadows the other values when the
    member matches the active template; otherwise we look at positions:
    both populated and target < current = downgrade, > current = upgrade,
    anything else = sidegrade (unordered group, or current template
    isn't a member).
    """
    if is_current:
        return "current"
    if current_position is None or target_position is None:
        return "sidegrade"
    if target_position < current_position:
        return "downgrade"
    if target_position > current_position:
        return "upgrade"
    return "sidegrade"


@router.get(
    "/billing/available-plans",
    response_model=AvailablePlansResponse,
    summary="List the plans the account can self-serve switch to",
    description=(
        "Return the set of templates this account is permitted to "
        "switch itself onto, derived from `BillingAccount.plan_group_id`. "
        "Empty list when the account has no group set or its group has "
        "no active members — the frontend uses that as the gate for "
        "hiding the 'Switch plan' section. Switches always land on the "
        "next AT_BOUNDARY (next-month start UTC); the timestamp is "
        "surfaced both at the top level and per-member for client "
        "convenience."
    ),
)
def list_available_plans(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> AvailablePlansResponse:
    from orchestra.db.dao.billing_plan_assignment_dao import next_month_boundary_utc
    from orchestra.db.dao.billing_plan_group_dao import BillingPlanGroupDAO

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    next_period = next_month_boundary_utc()
    next_period_iso = next_period.isoformat()

    # ``plan_group_id`` is NOT NULL (every account is on the
    # platform-default group at minimum, see DEFAULT_PLAN_GROUP_ID).
    # The two FE hide-rules below cover the previous "no group"
    # state uniformly:
    #   - active template not in group's members → no `is_current`
    #     → empty list (e.g. Enterprise customer + Default group);
    #   - group of one (only the current plan) → empty list.
    group_dao = BillingPlanGroupDAO(session)
    group = group_dao.get_by_id(ba.plan_group_id)
    members = group_dao.list_available_for_account(ba)

    # The "current" template's position drives downgrade detection
    # for every other member. Resolve once up-front rather than
    # per-row; positions live on PlanGroupMember rows already
    # loaded by ``list_available_for_account``.
    current_position: Optional[int] = next(
        (m.position for m in members if m.is_current),
        None,
    )

    # Pull pricing factors from the templates so the UI can show the
    # effective rate side-by-side with the current plan. We didn't put
    # them on PlanGroupAvailableMember to keep it minimal; load in one
    # batch query rather than N+1.
    from sqlalchemy import select as _select

    from orchestra.db.models.orchestra_models import BillingPlanTemplate

    template_rows = {
        t.id: t
        for t in session.execute(
            _select(BillingPlanTemplate).where(
                BillingPlanTemplate.id.in_([m.template_id for m in members]),
            ),
        )
        .scalars()
        .all()
    }

    items: list[AvailablePlanItem] = []
    for m in members:
        t = template_rows.get(m.template_id)
        items.append(
            AvailablePlanItem(
                template_id=m.template_id,
                template_name=m.template_name,
                template_display_name=m.template_display_name,
                billing_mode=m.billing_mode,
                commit_amount=m.commit_amount,
                currency=m.currency,
                commit_period=m.commit_period,
                commit_schedule=m.commit_schedule,
                base_pricing_factor=(
                    float(t.base_pricing_factor) if t is not None else 1.0
                ),
                overage_pricing_factor=(
                    float(t.overage_pricing_factor) if t is not None else 1.0
                ),
                position=m.position,
                is_current=m.is_current,
                classification=_classify_switch(
                    is_current=m.is_current,
                    current_position=current_position,
                    target_position=m.position,
                ),
                effective_at=next_period_iso,
            ),
        )

    # Two FE hide-rules implemented server-side so the frontend's
    # ``availablePlans.length === 0`` gate covers every "no useful
    # switching to do" case uniformly:
    #
    #   1. Misaligned state — the account's active template is not a
    #      member of its assigned group (e.g. an Enterprise customer
    #      pinned via setPlan but still on the platform-default group
    #      that only contains the default template). Surfacing
    #      "downgrade to Default" here would let the customer
    #      accidentally cancel their custom contract.
    #
    #   2. Group of one — the only entry is the customer's current
    #      plan, so there's nothing to switch to. This is the dominant
    #      state under the platform-default group today (members =
    #      [Default], current = Default → empty UX). When a paid tier
    #      joins the default group the rule no longer fires and every
    #      account suddenly sees the switcher.
    # Interval-aware: an account already subscribed to a paid tier may only
    # switch among tiers of the SAME billing interval (monthly↔annual goes
    # through cancel + resubscribe, never an in-place switch). Drop
    # other-interval members. Unsubscribed/default accounts have no interval
    # (commit_period is NULL) so both monthly and annual tiers are returned
    # and the picker's monthly/annual toggle decides what to show.
    current_member = next((m for m in members if m.is_current), None)
    current_interval = current_member.commit_period if current_member else None
    if current_interval in ("MONTHLY", "ANNUAL"):
        items = [
            it for it in items if it.commit_period == current_interval or it.is_current
        ]

    has_current = any(item.is_current for item in items)
    has_alternative = any(not item.is_current for item in items)
    if not has_current or not has_alternative:
        items = []

    return AvailablePlansResponse(
        billing_account_id=ba.id,
        plan_group_id=ba.plan_group_id,
        plan_group_display_name=(
            (group.display_name or group.name) if group is not None else None
        ),
        next_period_start=next_period_iso,
        available=items,
    )


@router.post(
    "/billing/plan",
    response_model=SwitchPlanResponse,
    summary="Self-serve switch to another plan in the account's plan group",
    description=(
        "Move the account to `template_id`, scheduled for the next "
        "AT_BOUNDARY (next-month start UTC). Refuses with 403 when "
        "`template_id` is not an active member of the account's "
        "assigned plan group — admins can still call "
        "`POST /v0/admin/billing/plan` directly for off-catalog moves. "
        "Idempotent: returns `status='noop'` when the requested "
        "template is the one the account is already on. Refunds are "
        "NOT applied automatically — UPFRONT mid-period changes that "
        "are billed but unused are surfaced by the reconciliation "
        "routine for manual operator handling."
    ),
)
def switch_plan(
    body: SwitchPlanRequest,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> SwitchPlanResponse:
    from orchestra.db.dao.billing_plan_assignment_dao import (
        BillingPlanAssignmentDAO,
        ConcurrentPlanChangeError,
        PendingRechargesError,
        TemplateNotAssignableError,
        next_month_boundary_utc,
    )
    from orchestra.db.dao.billing_plan_group_dao import (
        BillingPlanGroupDAO,
        PlanGroupMemberError,
    )
    from orchestra.db.models.enums import CollectionMethod
    from orchestra.db.models.orchestra_models import BillingMode, BillingPlanTemplate
    from orchestra.lib.billing import ensure_stripe_customer
    from orchestra.lib.subscription_billing import resolve_is_business

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    # The "owner + billing admin" policy is encoded in the
    # `billing:write` permission grant (the resource_access table
    # gives that permission to OWNER + the BILLING_ADMIN role and to
    # nobody else). Reusing it keeps the gating consistent with the
    # rest of the billing surface and avoids a parallel role check.
    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    # ``plan_group_id`` is NOT NULL — every account is on at least
    # the platform-default group. The membership check below is
    # therefore the only gate; off-group switches are refused
    # uniformly with 403 regardless of which group the account is on.
    group_dao = BillingPlanGroupDAO(session)
    if not group_dao.is_member(
        group_id=ba.plan_group_id,
        template_id=body.template_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Template id={body.template_id} is not part of this "
                "account's plan group; self-serve switch refused."
            ),
        )

    target_template = session.get(BillingPlanTemplate, body.template_id)
    if target_template is None or not target_template.is_active:
        # is_active=false members are filtered out of the ``list``
        # endpoint, but a stale client could still try; refuse rather
        # than letting the assignment DAO raise a less specific 400.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Template id={body.template_id} is not assignable "
                "(deprecated). Refresh the available-plans list."
            ),
        )

    plan_dao = BillingPlanAssignmentDAO(session)
    current = plan_dao.resolve_effective_plan(ba.id)

    # Determine classification (current/up/down/side) before any
    # mutation so the response is stable + the UI can always show the
    # confirmed direction.
    try:
        target_position = group_dao.get_member_position(
            group_id=ba.plan_group_id,
            template_id=body.template_id,
        )
    except PlanGroupMemberError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    try:
        current_position: Optional[int] = group_dao.get_member_position(
            group_id=ba.plan_group_id,
            template_id=current.template_id,
        )
    except PlanGroupMemberError:
        # Current template was moved off-group (admin override) —
        # unordered for classification purposes.
        current_position = None

    is_current = current.template_id == body.template_id
    classification = _classify_switch(
        is_current=is_current,
        current_position=current_position,
        target_position=target_position,
    )

    if is_current:
        return SwitchPlanResponse(
            status="noop",
            billing_account_id=ba.id,
            template_id=body.template_id,
            effective_at=None,
            classification="current",
        )

    # --- Self-serve subscription tier change (immediate) -----------------
    # Accounts already on a Stripe Subscription switch tiers *immediately*
    # (anniversary-anchored, not AT_BOUNDARY): the subscription quantity is
    # re-pointed with proration invoiced now, the cycle anchor resets to
    # now, and upgrades grant the credit delta inline. Downgrades are
    # allowed and never claw back consumed credits. METERED + free/default
    # accounts fall through to the legacy AT_BOUNDARY path below.
    if (
        ba.stripe_subscription_id
        and target_template.billing_mode == BillingMode.CREDITS
        and target_template.collection_method == CollectionMethod.STRIPE_SUBSCRIPTION
    ):
        from orchestra.lib.subscription_billing import (
            SubscriptionError,
            change_subscription_tier,
        )

        _init_stripe()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            change_subscription_tier(
                session,
                ba,
                target_template,
                user_id=user_id,
            )
        except SubscriptionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except stripe.error.CardError as exc:
            # The proration charge was declined. Because the tier change is
            # made with ``payment_behavior="error_if_incomplete"``, Stripe
            # rolled the subscription back and we never touched the local
            # plan/credits — so this is a clean "try another card" failure
            # rather than a half-applied upgrade.
            logger.info(f"Tier change declined by card: {exc}")
            raise HTTPException(
                status_code=402,
                detail=(
                    "Your card was declined, so the plan change didn't go "
                    "through and you're still on your current plan. Update "
                    "your default card under Manage payment methods and try "
                    "again."
                ),
            )
        except stripe.error.StripeError as exc:
            logger.error(f"Stripe error changing tier: {exc}", exc_info=True)
            raise HTTPException(
                status_code=502,
                detail=f"Stripe error while changing subscription tier: {exc}",
            )
        session.commit()
        return SwitchPlanResponse(
            status="switched",
            billing_account_id=ba.id,
            template_id=body.template_id,
            effective_at=now_iso,
            classification=classification,
        )

    # METERED templates need a Stripe Customer for the invoicer to
    # attach monthly invoices. Mirror the admin endpoint's behaviour
    # but auto-create silently — the customer has already gone
    # through Buy-Credits / billing-profile setup at this point so
    # the metadata is in place; making them call a separate endpoint
    # first would be a UX regression.
    if (
        target_template.billing_mode == BillingMode.METERED
        and not ba.stripe_customer_id
    ):
        try:
            ensure_stripe_customer(
                session,
                ba,
                is_business=resolve_is_business(ba, organization_id),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Stripe error while creating Customer: {exc}",
            )

    next_boundary = next_month_boundary_utc()
    try:
        assignment = plan_dao.set_plan(
            billing_account_id=ba.id,
            template_id=body.template_id,
            created_by_user_id=user_id,
            change_reason=(
                body.change_reason
                or f"self-serve switch ({classification}) by user {user_id}"
            ),
            effective_at=next_boundary,
        )
    except TemplateNotAssignableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PendingRechargesError as exc:
        # Self-serve customers shouldn't see internal recharge ids — the
        # message is the same shape as the admin response so the FE can
        # share rendering, but tells the customer to retry shortly
        # rather than offering a drain affordance.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pending_recharges",
                "message": (
                    "A pending charge is still being invoiced "
                    "on your current plan. Try switching again after "
                    "your next monthly invoice has been issued."
                ),
                "pending_recharge_ids": exc.pending_recharge_ids,
            },
        )
    except ConcurrentPlanChangeError:
        # Race with another writer (typically two browser tabs or a
        # double-click). Tell the customer to refresh and retry —
        # we deliberately don't expose internal account ids in the
        # self-serve response.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "concurrent_plan_change",
                "message": (
                    "Another change to your plan is in progress. "
                    "Please refresh the page and try again."
                ),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    session.commit()

    if assignment is None:
        # set_plan only returns None for the "already on this
        # template" idempotency path — we already short-circuited
        # that above, so this branch is defensive.
        return SwitchPlanResponse(
            status="noop",
            billing_account_id=ba.id,
            template_id=body.template_id,
            effective_at=None,
            classification="current",
        )

    return SwitchPlanResponse(
        status="scheduled",
        billing_account_id=ba.id,
        template_id=body.template_id,
        effective_at=next_boundary.isoformat(),
        classification=classification,
    )


# ============================================================================
# POST /billing/subscribe  — self-serve subscription onboarding
# ============================================================================


@router.post(
    "/billing/subscribe",
    response_model=SubscribeResponse,
    summary="Subscribe a self-serve account to a credit tier",
    description=(
        "Create the backing Stripe Subscription for a self-serve CREDITS "
        "tier and activate the plan immediately. The monthly credit grant "
        "is posted once Stripe collects the first invoice (via the "
        "``invoice.paid`` webhook). Refuses if the account is already on a "
        "subscription (use ``POST /v0/billing/plan`` to upgrade/downgrade) "
        "or if the target is not an active self-serve tier in the "
        "account's plan group."
    ),
)
def subscribe(
    body: SubscribeRequest,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> SubscribeResponse:
    from orchestra.db.dao.billing_plan_group_dao import BillingPlanGroupDAO
    from orchestra.db.models.orchestra_models import BillingPlanTemplate
    from orchestra.lib.subscription_billing import (
        SubscriptionError,
        assert_self_serve_subscribable,
        create_subscription,
        resolve_is_business,
    )

    _init_stripe()

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    if ba.stripe_subscription_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Account already has an active subscription. Use the plan "
                "switch endpoint to change tiers."
            ),
        )

    # Membership gate (same as switch_plan): the tier must be in the
    # account's plan group.
    group_dao = BillingPlanGroupDAO(session)
    if not group_dao.is_member(
        group_id=ba.plan_group_id,
        template_id=body.template_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Template id={body.template_id} is not part of this "
                "account's plan group; subscribe refused."
            ),
        )

    template = session.get(BillingPlanTemplate, body.template_id)
    if template is None or not template.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"Template id={body.template_id} is not assignable.",
        )
    try:
        assert_self_serve_subscribable(template)
    except SubscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Tax gate: subscriptions enable Stripe automatic tax, which needs the
    # customer's location resolvable at invoice time. The address lives on the
    # Stripe Customer (synced from the Billing Profile); the local
    # ``billing_setup_complete`` flag is the derived (non-PII) mirror of that
    # address's completeness, kept fresh on every mutation path (profile PATCH,
    # admin edit, and the ``customer.updated`` webhook for dashboard edits), so
    # we gate on it without a Stripe round-trip and it can't drift stale.
    # A country alone resolves tax for country-level jurisdictions (e.g. UK
    # VAT) but NOT for sub-national ones (US sales tax needs a postal
    # code/state), so a full address is required — otherwise the first invoice
    # can't finalise (automatic_tax -> requires_location_inputs).
    if not ba.billing_setup_complete:
        raise HTTPException(
            status_code=400,
            detail=(
                "Add your full billing address (street, city, postal code and "
                "country) in your Billing Profile before subscribing — it's "
                "needed to calculate tax on your invoices."
            ),
        )

    user = UserDAO(session).get_user_with_id(user_id)
    fallback_email = user.email if user else None
    fallback_name = user.name if user else None

    from orchestra.lib.billing import PaymentMethodError

    try:
        subscription = create_subscription(
            session,
            ba,
            template,
            is_business=resolve_is_business(ba, organization_id),
            user_id=user_id,
            organization_id=organization_id,
            fallback_email=fallback_email,
            fallback_name=fallback_name,
        )
    except PaymentMethodError:
        # No saved card to charge off-session — the in-app payment manager
        # lets the customer add one before subscribing.
        raise HTTPException(
            status_code=402,
            detail=(
                "Add a payment method under Payment methods before "
                "subscribing — we charge your first invoice right away."
            ),
        )
    except stripe.error.CardError as exc:
        # The off-session first charge was declined; ``error_if_incomplete``
        # means no subscription was created, so the account stays on its
        # current (free) plan.
        logger.info(f"Subscribe declined by card: {exc}")
        raise HTTPException(
            status_code=402,
            detail=(
                "Your card was declined, so the subscription wasn't started. "
                "Update your default card under Payment methods and try again."
            ),
        )
    except SubscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.InvalidRequestError as exc:
        # Bad customer input (not an upstream outage) — most commonly Stripe
        # Tax failing to resolve the customer's location from the saved
        # address (invalid/incomplete postal code, or a US address with no
        # state). Surface a clean, actionable 400 instead of a 502.
        msg = str(exc)
        logger.warning(f"Stripe rejected subscription (invalid request): {msg}")
        if (
            "location" in msg.lower()
            or "address" in msg.lower()
            or "tax" in msg.lower()
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "We couldn't verify your billing address for tax. Please "
                    "double-check it's a valid, complete address — including a "
                    "correct postal/ZIP code and, for US addresses, a state — "
                    "then try again."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail=f"Stripe rejected the subscription: {msg}",
        )
    except stripe.error.StripeError as exc:
        logger.error(f"Stripe error creating subscription: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"Stripe error while creating subscription: {exc}",
        )

    session.commit()

    # Surface the latest invoice's PaymentIntent client secret / hosted
    # URL so the FE can collect payment if the customer has no usable
    # default PM yet (default_incomplete flow).
    client_secret: Optional[str] = None
    hosted_invoice_url: Optional[str] = None
    latest_invoice = (
        subscription.get("latest_invoice")
        if isinstance(subscription, dict)
        else getattr(subscription, "latest_invoice", None)
    )
    if isinstance(latest_invoice, dict):
        hosted_invoice_url = latest_invoice.get("hosted_invoice_url")
        payment_intent = latest_invoice.get("payment_intent")
        if isinstance(payment_intent, dict):
            client_secret = payment_intent.get("client_secret")

    return SubscribeResponse(
        status="subscribed",
        billing_account_id=ba.id,
        template_id=body.template_id,
        stripe_subscription_id=ba.stripe_subscription_id or "",
        subscription_status=(
            subscription.get("status")
            if isinstance(subscription, dict)
            else getattr(subscription, "status", None)
        ),
        client_secret=client_secret,
        hosted_invoice_url=hosted_invoice_url,
    )


# ============================================================================
# DELETE /billing/subscription  — self-serve cancellation
# ============================================================================


@router.delete(
    "/billing/subscription",
    response_model=CancelSubscriptionResponse,
    summary="Cancel a self-serve subscription",
    description=(
        "Cancel the account's self-serve subscription. By default the "
        "cancellation is scheduled for the end of the current billing "
        "period (the customer keeps their credits and service until "
        "then); pass ``immediate=true`` to cancel right away (forfeiting "
        "any unconsumed credits). The account reverts to the free tier "
        "when Stripe emits the deletion webhook."
    ),
)
def cancel_subscription_endpoint(
    request_fastapi: Request,
    immediate: bool = False,
    session: Session = Depends(get_db_session),
) -> CancelSubscriptionResponse:
    from orchestra.lib.subscription_billing import (
        SubscriptionError,
        cancel_subscription,
    )

    _init_stripe()

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    if not ba.stripe_subscription_id:
        raise HTTPException(
            status_code=400,
            detail="Account has no active subscription to cancel.",
        )

    try:
        effective = cancel_subscription(
            session,
            ba,
            at_period_end=not immediate,
        )
    except SubscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.StripeError as exc:
        logger.error(f"Stripe error cancelling subscription: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"Stripe error while cancelling subscription: {exc}",
        )

    session.commit()

    return CancelSubscriptionResponse(
        status="canceled" if immediate else "canceling",
        billing_account_id=ba.id,
        effective_at=effective.isoformat() if effective else None,
    )


# ============================================================================
# POST /billing/subscription/reactivate  — undo a scheduled cancellation
# ============================================================================


@router.post(
    "/billing/subscription/reactivate",
    response_model=CancelSubscriptionResponse,
    summary="Resume a subscription scheduled to cancel",
    description=(
        "Clears a pending end-of-period cancellation so the subscription "
        "renews normally. Only valid while the subscription is still active "
        "and flagged to cancel at period end (before Stripe deletes it at the "
        "period boundary)."
    ),
)
def reactivate_subscription_endpoint(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> CancelSubscriptionResponse:
    from orchestra.lib.subscription_billing import (
        SubscriptionError,
        reactivate_subscription,
    )

    _init_stripe()

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )
    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    ba = BillingAccountDAO(session).resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    if not ba.stripe_subscription_id:
        raise HTTPException(
            status_code=400,
            detail="Account has no active subscription to resume.",
        )

    if not ba.subscription_cancel_at_period_end:
        raise HTTPException(
            status_code=400,
            detail="Subscription is not scheduled to cancel.",
        )

    try:
        effective = reactivate_subscription(session, ba)
    except SubscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.StripeError as exc:
        logger.error(
            f"Stripe error reactivating subscription: {exc}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Stripe error while resuming subscription: {exc}",
        )

    session.commit()

    return CancelSubscriptionResponse(
        status="active",
        billing_account_id=ba.id,
        effective_at=effective.isoformat() if effective else None,
    )
