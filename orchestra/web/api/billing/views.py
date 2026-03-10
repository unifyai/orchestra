"""
Billing API endpoints – Stripe checkout, portal, status, billing profiles,
tax validation, and organization billing management.

These endpoints replace direct Stripe SDK calls that were previously made by
the console frontend.  The frontend now calls these thin wrappers instead,
keeping the Stripe secret key exclusively on the backend.
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.param_functions import Depends
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import (
    MIN_AUTORECHARGE_AMOUNT,
    MIN_SPEND_FOR_AUTO_RECHARGE,
    BillingAccountDAO,
)
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.lib.billing import (
    COUNTRY_NAMES,
    configure_stripe,
    extract_tax_id_info,
    is_stripe_mode_conflict,
    prefill_customer_fields,
    sync_billing_profile_to_stripe,
    sync_tax_id_to_customer,
)
from orchestra.settings import settings
from orchestra.web.api.billing.schema import (
    AccountInfoResponse,
    AutoRechargeResponse,
    AutoRechargeUpdateRequest,
    BillingProfileResponse,
    BillingProfileUpdate,
    CheckoutSessionResponse,
    CheckoutStatusResponse,
    PortalSessionResponse,
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
        raise HTTPException(status_code=500, detail=str(exc))


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
    account status, and auto-recharge settings.  Context (personal vs org)
    is derived from the API key.
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

    return AccountInfoResponse(
        billing_account_id=ba.id,
        credits=float(ba.credits) if ba.credits else 0.0,
        account_status=ba.account_status or "ACTIVE",
        last_recharge_at=last_recharge_at,
        autorecharge=ba.autorecharge,
        autorecharge_threshold=(
            float(ba.autorecharge_threshold) if ba.autorecharge_threshold else 0.0
        ),
        autorecharge_qty=(float(ba.autorecharge_qty) if ba.autorecharge_qty else 25.0),
    )


# ============================================================================
# POST /billing/checkout-session
# ============================================================================


@router.post(
    "/billing/checkout-session",
    response_model=CheckoutSessionResponse,
    responses={
        200: {"description": "Checkout session created"},
        400: {"description": "Bad request"},
        500: {"description": "Stripe configuration error"},
    },
)
def create_checkout_session(
    request_fastapi: Request,
    session=Depends(get_db_session),
) -> CheckoutSessionResponse:
    """
    Create a Stripe Checkout session for the authenticated user / org.

    Resolves billing context from the API key, fetches user & billing data,
    creates a Stripe Checkout Session, and returns the redirect URL + session
    ID.
    """
    _init_stripe()

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )

    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    # --- Resolve billing account -----------------------------------------
    user_dao = UserDAO(session)
    ba_dao = BillingAccountDAO(session)

    user = user_dao.get_user_with_id(user_id)

    if organization_id:
        org_dao = OrganizationDAO(session)
        org = org_dao.get(organization_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        ba = org.billing_account
        if ba is None:
            ba = ba_dao.create()
            org.billing_account_id = ba.id
            session.flush()
    else:
        ba = user.billing_account
        if ba is None:
            ba = ba_dao.create()
            user.billing_account_id = ba.id
            session.flush()

    customer_id: Optional[str] = ba.stripe_customer_id

    # --- Collect checkout metadata ---------------------------------------
    # Total spending (for fraud / repeat-customer signals)
    total_spending = float(ba_dao.get_total_spending(ba.id))

    # Account age
    account_age_days = 0
    if user.created_at:
        created = user.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        account_age_days = math.floor(
            (now - created).total_seconds() / 86400,
        )

    is_repeat_customer = total_spending > 0

    # Billing profile (tax / name / email)
    billing_email: Optional[str] = ba.billing_email or user.email
    billing_name: Optional[str] = ba.name or user.name
    tax_id: Optional[str] = ba.tax_id
    tax_id_type: Optional[str] = ba.tax_id_type or "eu_vat"
    has_tax_id = bool(tax_id)

    # --- Checkout quantities ---------------------------------------------
    default_credit_qty = getattr(settings, "stripe_default_credit_qty", 25)
    min_credit_qty = getattr(settings, "stripe_min_credit_qty", 5)
    max_credit_qty = getattr(settings, "stripe_max_credit_qty", 500)

    # --- Price ID --------------------------------------------------------
    price_id = (
        settings.stripe_unify_credits_price_id_business
        if organization_id
        else settings.stripe_unify_credits_price_id_personal
    )
    if not price_id:
        ctx_label = "business" if organization_id else "personal"
        raise HTTPException(
            status_code=500,
            detail=(
                f"Stripe price ID not configured for {ctx_label} workspace. "
                "Check STRIPE_UNIFY_CREDITS_PRICE_ID_PERSONAL / _BUSINESS env vars."
            ),
        )

    # --- Pre-fill / tax sync on existing customer ------------------------
    if customer_id:
        prefill_customer_fields(customer_id, billing_email, billing_name)
        if has_tax_id and tax_id:
            sync_tax_id_to_customer(customer_id, tax_id, tax_id_type)

    # --- Session metadata ------------------------------------------------
    metadata: dict = {
        "user_id": user_id,
        "credits_purchased": str(default_credit_qty),
        "user_total_spend": str(total_spending),
        "user_account_age_days": str(account_age_days),
        "user_is_repeat_customer": str(is_repeat_customer),
    }
    if organization_id:
        metadata["organization_id"] = str(organization_id)

    # --- Build session params --------------------------------------------
    console_url = settings.console_url.rstrip("/")

    session_params: dict = {
        "mode": "payment",
        "submit_type": "pay",
        "line_items": [
            {
                "price": price_id,
                "quantity": default_credit_qty,
                "adjustable_quantity": {
                    "enabled": True,
                    "minimum": min_credit_qty,
                    "maximum": max_credit_qty,
                },
            },
        ],
        "payment_method_types": ["card"],
        "automatic_tax": {"enabled": True},
        "client_reference_id": user_id,
        "success_url": f"{console_url}/billing?sessionId={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{console_url}/billing",
        "billing_address_collection": "required",
        "payment_method_options": {
            "card": {"request_three_d_secure": "automatic"},
        },
        "custom_text": {
            "submit": {
                "message": "Credits will be added to your account immediately after payment.",
            },
        },
        "payment_intent_data": {"metadata": metadata},
        "metadata": metadata,
    }

    if has_tax_id:
        session_params["tax_id_collection"] = {"enabled": True}

    if customer_id:
        session_params["customer"] = customer_id
        session_params["customer_update"] = {"address": "auto", "name": "auto"}
    else:
        session_params["customer_creation"] = "always"
        if billing_email:
            session_params["customer_email"] = billing_email

    # --- Create the Stripe Checkout Session ------------------------------
    try:
        checkout_session = stripe.checkout.Session.create(**session_params)
    except stripe.InvalidRequestError as exc:
        if customer_id and is_stripe_mode_conflict(exc):
            logger.warning(
                "Customer %s is from a different Stripe mode; retrying without customer",
                customer_id,
            )
            session_params.pop("customer", None)
            session_params.pop("customer_update", None)
            session_params["customer_creation"] = "always"
            if billing_email:
                session_params["customer_email"] = billing_email
            checkout_session = stripe.checkout.Session.create(**session_params)
        else:
            raise HTTPException(status_code=400, detail=str(exc))

    if not checkout_session.url:
        raise HTTPException(
            status_code=500,
            detail="Failed to create checkout session URL",
        )

    return CheckoutSessionResponse(
        url=checkout_session.url,
        session_id=checkout_session.id,
    )


# ============================================================================
# POST /billing/portal-session
# ============================================================================


@router.post(
    "/billing/portal-session",
    response_model=PortalSessionResponse,
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

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
        )
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
        raise HTTPException(status_code=400, detail=str(exc))

    return PortalSessionResponse(url=portal_session.url)


# ============================================================================
# GET /billing/checkout-status
# ============================================================================


@router.get(
    "/billing/checkout-status",
    response_model=CheckoutStatusResponse,
    responses={
        200: {"description": "Checkout session status"},
        400: {"description": "Missing or invalid sessionId"},
        403: {"description": "Session does not belong to caller"},
        500: {"description": "Stripe configuration error"},
    },
)
def get_checkout_status(
    request_fastapi: Request,
    session_id: str,
    session=Depends(get_db_session),
) -> CheckoutStatusResponse:
    """
    Retrieve the status of a Stripe Checkout session.

    Security: verifies the session belongs to the authenticated user / org
    by cross-checking the Stripe customer or ``client_reference_id``.
    """
    _init_stripe()

    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )

    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    # Get billing account to find stripe customer
    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")
    customer_id: Optional[str] = ba.stripe_customer_id

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except stripe.InvalidRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # --- Ownership verification ------------------------------------------
    session_customer = checkout_session.customer
    session_ref_id = checkout_session.client_reference_id

    if customer_id and session_customer != customer_id:
        # Also allow match via client_reference_id for the authenticated user
        if session_ref_id != user_id:
            raise HTTPException(
                status_code=403,
                detail="Checkout session does not belong to the authenticated workspace",
            )
    elif not customer_id and session_ref_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Checkout session does not belong to the authenticated workspace",
        )

    return CheckoutStatusResponse(
        status=checkout_session.status,
        payment_status=checkout_session.payment_status,
    )


# ============================================================================
# GET / PUT  /billing/auto-recharge
# ============================================================================


@router.get(
    "/billing/auto-recharge",
    response_model=AutoRechargeResponse,
    responses={
        200: {"description": "Auto-recharge settings and eligibility"},
        400: {"description": "Billing not set up"},
    },
)
def get_auto_recharge(
    request_fastapi: Request,
    session=Depends(get_db_session),
) -> AutoRechargeResponse:
    """
    Return auto-recharge settings **and** eligibility in a single call.

    The response includes:
    - Current settings (``enabled``, ``threshold``, ``qty``).
    - Eligibility data (``eligible``, ``total_spending``,
      ``minimum_spend_required``, ``remaining_spend_needed``).

    Context (personal vs org) is derived from the API key.
    """
    user_id: str = request_fastapi.state.user_id
    organization_id: Optional[int] = getattr(
        request_fastapi.state,
        "organization_id",
        None,
    )

    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)
    if not ba:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    total_spending = float(ba_dao.get_total_spending(ba.id))
    can_enable = ba_dao.can_enable_auto_recharge(ba.id)
    min_required = float(MIN_SPEND_FOR_AUTO_RECHARGE)

    return AutoRechargeResponse(
        enabled=ba.autorecharge,
        threshold=float(ba.autorecharge_threshold),
        qty=float(ba.autorecharge_qty),
        min_recharge_amount=float(MIN_AUTORECHARGE_AMOUNT),
        eligible=can_enable,
        total_spending=total_spending,
        minimum_spend_required=min_required,
        remaining_spend_needed=max(0.0, min_required - total_spending),
    )


@router.put(
    "/billing/auto-recharge",
    response_model=AutoRechargeResponse,
    responses={
        200: {"description": "Auto-recharge settings updated"},
        400: {"description": "Validation error or eligibility not met"},
    },
)
def update_auto_recharge(
    request_fastapi: Request,
    body: AutoRechargeUpdateRequest,
    session=Depends(get_db_session),
) -> AutoRechargeResponse:
    """
    Update auto-recharge settings atomically.

    - ``enabled`` (required) – enable or disable auto-recharge.
    - ``threshold`` (optional) – the credit balance that triggers a top-up.
    - ``qty`` (optional) – the amount of credits to add per top-up.

    When *enabling*, the account must have met the minimum spending
    threshold (fraud-prevention measure).  ``qty`` must be ≥ $25.

    Context (personal vs org) is derived from the API key.
    Returns the updated settings + eligibility (same shape as GET).
    """
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

    # --- Eligibility check when enabling ---------------------------------
    if body.enabled and not ba.autorecharge:
        if not ba_dao.can_enable_auto_recharge(ba.id):
            total_spending = float(ba_dao.get_total_spending(ba.id))
            min_required = float(MIN_SPEND_FOR_AUTO_RECHARGE)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"You must spend at least ${min_required:.2f} before "
                    f"enabling auto-recharge. "
                    f"Current spending: ${total_spending:.2f}"
                ),
            )

    # --- Validate qty if provided ----------------------------------------
    if body.qty is not None and body.qty < float(MIN_AUTORECHARGE_AMOUNT):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Minimum auto-recharge amount is "
                f"${float(MIN_AUTORECHARGE_AMOUNT):.2f}. "
                f"Provided: ${body.qty:.2f}"
            ),
        )

    # --- Apply updates ---------------------------------------------------
    ba.autorecharge = body.enabled

    if body.threshold is not None:
        ba.autorecharge_threshold = Decimal(str(body.threshold))

    if body.qty is not None:
        ba.autorecharge_qty = Decimal(str(body.qty))

    session.commit()
    session.refresh(ba)

    # --- Return updated state + eligibility ------------------------------
    total_spending = float(ba_dao.get_total_spending(ba.id))
    can_enable = ba_dao.can_enable_auto_recharge(ba.id)
    min_required = float(MIN_SPEND_FOR_AUTO_RECHARGE)

    return AutoRechargeResponse(
        enabled=ba.autorecharge,
        threshold=float(ba.autorecharge_threshold),
        qty=float(ba.autorecharge_qty),
        min_recharge_amount=float(MIN_AUTORECHARGE_AMOUNT),
        eligible=can_enable,
        total_spending=total_spending,
        minimum_spend_required=min_required,
        remaining_spend_needed=max(0.0, min_required - total_spending),
    )


# ============================================================================
# Tax Validation Endpoints (moved from users/views.py)
# ============================================================================


@router.post("/billing/validate-tax-id")
def validate_tax_id(
    request: Request,
    tax_id: str = Query(..., description="Tax ID to validate"),
    country: str = Query(..., description="Two-letter country code"),
    session: Session = Depends(get_db_session),
):
    """Validate a tax ID format for a specific country."""
    try:
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
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")


@router.get("/billing/supported-tax-countries")
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

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)

    is_business = organization_id is not None

    if not ba:
        return BillingProfileResponse(is_business=is_business)

    profile = ba_dao.get_billing_profile(ba.id)
    if not profile:
        return BillingProfileResponse(is_business=is_business)

    name = profile.get("name")

    return BillingProfileResponse(
        billing_email=profile.get("billing_email"),
        name=name,
        individual_name=name if not is_business else None,
        business_name=name,
        tax_id=profile.get("tax_id"),
        tax_id_type=profile.get("tax_id_type"),
        billing_address=profile.get("billing_address", {}),
        billing_setup_complete=profile.get("billing_setup_complete", False),
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

    is_business = organization_id is not None

    billing_email = profile_update.billing_email
    resolved_name = profile_update.resolved_name
    tax_id = profile_update.tax_id
    tax_id_type = profile_update.tax_id_type
    billing_address = profile_update.billing_address

    # Validate billing address if provided
    if billing_address is not None:
        from orchestra.web.api.utils.business_validation import (
            validate_billing_address_data,
        )

        addr = billing_address if isinstance(billing_address, dict) else {}
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

    # Validate tax_id if provided along with country
    existing_billing_address = ba.billing_address
    if tax_id is not None:
        country = None
        if billing_address and billing_address.get("country"):
            country = billing_address["country"]
        elif existing_billing_address and existing_billing_address.get("country"):
            country = existing_billing_address["country"]

        if country:
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

    # Persist changes via DAO
    ba_dao.update_billing_profile(
        billing_account_id=ba.id,
        billing_email=billing_email,
        name=resolved_name,
        tax_id=tax_id,
        tax_id_type=tax_id_type,
        billing_address=billing_address,
    )
    session.flush()

    # Sync to Stripe if customer exists
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

    session.commit()

    # Build response
    profile = ba_dao.get_billing_profile(ba.id)
    name = profile.get("name") if profile else None

    return BillingProfileResponse(
        billing_email=profile.get("billing_email") if profile else None,
        name=name,
        individual_name=name if not is_business else None,
        business_name=name,
        tax_id=profile.get("tax_id") if profile else None,
        tax_id_type=profile.get("tax_id_type") if profile else None,
        billing_address=profile.get("billing_address", {}) if profile else {},
        billing_setup_complete=(
            profile.get("billing_setup_complete", False) if profile else False
        ),
        is_business=is_business,
    )
