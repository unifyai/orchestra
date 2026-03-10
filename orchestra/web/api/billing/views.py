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
from fastapi import APIRouter, Body, HTTPException, Query, Request, status
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
    configure_stripe,
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
    BillingAddress,
    CheckoutSessionResponse,
    CheckoutStatusResponse,
    OrganizationBillingResponse,
    OrganizationBillingUpdate,
    OrganizationBusinessProfileResponse,
    OrganizationBusinessProfileUpdate,
    OrganizationCreditsResponse,
    OrganizationStripeCustomerCreateRequest,
    OrganizationStripeCustomerResponse,
    PortalSessionResponse,
    UserBillingProfileResponse,
    UserBillingProfileUpdate,
)
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
        eligible=can_enable,
        total_spending=total_spending,
        minimum_spend_required=min_required,
        remaining_spend_needed=max(0.0, min_required - total_spending),
    )


# ============================================================================
# Tax Validation Endpoints (moved from users/views.py)
# ============================================================================


@router.post("/billing/validate-tax-id")
@router.post("/user/validate-tax-id")  # backward-compat alias
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
@router.get("/user/supported-tax-countries")  # backward-compat alias
def get_supported_tax_countries():
    """Get list of countries supported for tax ID validation."""
    return {
        "supported_countries": TaxIDValidator.get_supported_countries(),
        "total_countries": len(TaxIDValidator.get_supported_countries()),
    }


# ============================================================================
# User Billing Profile Endpoints (moved from users/views.py)
# ============================================================================


@router.get(
    "/user/billing/billing-profile",
    response_model=UserBillingProfileResponse,
    summary="Get user business profile",
    description="Get the current user's billing/business profile information.",
)
def get_user_billing_profile(
    request: Request,
    session: Session = Depends(get_db_session),
) -> UserBillingProfileResponse:
    """
    Get the current user's billing profile.

    Returns billing_email, individual_name (+ business_name alias),
    tax_id, tax_id_type, billing_address from the user's BillingAccount.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)
    user = user_dao.get_user_with_id(user_id)

    ba = user.billing_account
    if not ba:
        return UserBillingProfileResponse()

    billing_account_dao = BillingAccountDAO(session)
    profile = billing_account_dao.get_billing_profile(ba.id)
    if not profile:
        return UserBillingProfileResponse()

    # Map DAO's generic "name" to individual_name + backward-compat alias
    name = profile.pop("name", None)
    profile["individual_name"] = name
    profile["business_name"] = name  # backward-compat alias
    return UserBillingProfileResponse(**profile)


@router.patch(
    "/user/billing/billing-profile",
    response_model=UserBillingProfileResponse,
    summary="Update user business profile",
    description="Update the current user's billing/business profile information.",
)
def update_user_billing_profile(
    request: Request,
    profile_update: UserBillingProfileUpdate,
    session: Session = Depends(get_db_session),
) -> UserBillingProfileResponse:
    """
    Update the current user's business profile (billing details).

    Only provided fields are updated. Billing address is merged with existing data.
    Also syncs changes to Stripe customer if one exists.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)
    user = user_dao.get_user_with_id(user_id)

    ba = user.billing_account
    if not ba:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no billing account",
        )

    resolved_name = profile_update.resolved_name

    # Validate billing address if provided
    if profile_update.billing_address is not None:
        from orchestra.web.api.utils.business_validation import (
            validate_billing_address_data,
        )

        addr = (
            profile_update.billing_address
            if isinstance(profile_update.billing_address, dict)
            else {}
        )
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

    billing_account_dao = BillingAccountDAO(session)
    billing_account_dao.update_billing_profile(
        billing_account_id=ba.id,
        billing_email=profile_update.billing_email,
        name=resolved_name,
        tax_id=profile_update.tax_id,
        tax_id_type=profile_update.tax_id_type,
        billing_address=profile_update.billing_address,
    )
    session.flush()

    # Sync to Stripe if customer exists
    if ba.stripe_customer_id:
        billing_address_dict = (
            (
                profile_update.billing_address
                if isinstance(profile_update.billing_address, dict)
                else None
            )
            if profile_update.billing_address is not None
            else None
        )

        sync_billing_profile_to_stripe(
            ba.stripe_customer_id,
            is_business=False,
            billing_email=profile_update.billing_email,
            name=resolved_name,
            tax_id=profile_update.tax_id,
            billing_address=billing_address_dict,
            existing_billing_address=ba.billing_address,
            logger_instance=logger,
        )

    session.commit()

    profile = billing_account_dao.get_billing_profile(ba.id)
    name = profile.pop("name", None)
    profile["individual_name"] = name
    profile["business_name"] = name  # backward-compat alias
    return UserBillingProfileResponse(**profile)


# ============================================================================
# Organization Billing Endpoints (moved from organization/views.py)
# ============================================================================


@router.get(
    "/organizations/{organization_id}/billing",
    tags=["organization-billing"],
)
async def get_organization_billing(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get billing information for an organization.

    Returns credits, billing settings, and account status.
    Requires billing:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:read permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view billing for this organization",
        )

    ba = org.billing_account

    return OrganizationBillingResponse(
        organization_id=organization_id,
        organization_name=org.name,
        credits=float(ba.credits) if ba else 0.0,
        stripe_customer_id=ba.stripe_customer_id if ba else None,
        autorecharge=ba.autorecharge if ba else False,
        autorecharge_threshold=float(ba.autorecharge_threshold) if ba else 0.0,
        autorecharge_qty=float(ba.autorecharge_qty) if ba else 25.0,
        account_status=ba.account_status if ba else "ACTIVE",
        billing_setup_complete=ba.billing_setup_complete if ba else False,
    ).model_dump()


@router.patch(
    "/organizations/{organization_id}/billing",
    tags=["organization-billing"],
)
async def update_organization_billing(
    request_fastapi: Request,
    organization_id: int,
    billing_update: OrganizationBillingUpdate,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Update billing settings for an organization.

    Requires billing:write permission.
    Owners and Admins have this permission by default.
    """
    import decimal

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    ba_dao = BillingAccountDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:write permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update billing settings",
        )

    # Get billing account (created eagerly in organization_dao.create;
    # fallback handles legacy orgs that may not have one yet)
    ba = org.billing_account
    if ba is None:
        ba = ba_dao.create()
        org.billing_account_id = ba.id
        session.flush()

    # Update settings directly on BillingAccount
    if billing_update.autorecharge is not None:
        ba.autorecharge = billing_update.autorecharge

    if billing_update.autorecharge_threshold is not None:
        ba.autorecharge_threshold = decimal.Decimal(
            str(billing_update.autorecharge_threshold),
        )

    if billing_update.autorecharge_qty is not None:
        qty = decimal.Decimal(str(billing_update.autorecharge_qty))
        if qty < MIN_AUTORECHARGE_AMOUNT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Minimum auto-recharge amount is ${MIN_AUTORECHARGE_AMOUNT}.",
            )
        ba.autorecharge_qty = qty

    session.commit()

    # Return updated billing info via BillingAccount
    session.refresh(org)
    ba = org.billing_account

    return OrganizationBillingResponse(
        organization_id=organization_id,
        organization_name=org.name,
        credits=float(ba.credits) if ba else 0.0,
        stripe_customer_id=ba.stripe_customer_id if ba else None,
        autorecharge=ba.autorecharge if ba else False,
        autorecharge_threshold=float(ba.autorecharge_threshold) if ba else 0.0,
        autorecharge_qty=float(ba.autorecharge_qty) if ba else 25.0,
        account_status=ba.account_status if ba else "ACTIVE",
        billing_setup_complete=ba.billing_setup_complete if ba else False,
    ).model_dump()


@router.get(
    "/organizations/{organization_id}/billing/credits",
    tags=["organization-billing"],
)
async def get_organization_credits(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get credit balance for an organization.

    For direct billing orgs, returns the org's credit balance.
    For orgs without billing configured, returns 0.
    Requires billing:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:read permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view credits for this organization",
        )

    # Get credits from organization's BillingAccount
    ba = org.billing_account
    has_direct = ba is not None and ba.stripe_customer_id is not None
    credits = float(ba.credits) if has_direct else 0.0

    return OrganizationCreditsResponse(
        organization_id=organization_id,
        credits=credits,
    ).model_dump()


# ============================================================================
# Organization Billing Profile Endpoints (moved from organization/views.py)
# ============================================================================


@router.get(
    "/organizations/{organization_id}/billing/billing-profile",
    tags=["organization-billing"],
)
async def get_organization_billing_profile(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get business profile for an organization (invoicing information).

    Requires billing:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:read permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view business profile",
        )

    # Get business profile directly from BillingAccount
    ba = org.billing_account
    profile = {
        "billing_email": ba.billing_email if ba else None,
        "business_name": ba.name if ba else None,
        "tax_id": ba.tax_id if ba else None,
        "billing_address": ba.billing_address if ba else None,
    }
    return OrganizationBusinessProfileResponse(**profile).model_dump()


@router.patch(
    "/organizations/{organization_id}/billing/billing-profile",
    tags=["organization-billing"],
)
async def update_organization_billing_profile(
    request_fastapi: Request,
    organization_id: int,
    profile_update: OrganizationBusinessProfileUpdate,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Update business profile for an organization.

    Requires billing:write permission.
    Owners and Admins have this permission by default.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    ba_dao = BillingAccountDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:write permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update business profile",
        )

    # Get billing account (created eagerly in organization_dao.create;
    # fallback handles legacy orgs that may not have one yet)
    ba = org.billing_account
    if ba is None:
        ba = ba_dao.create()
        org.billing_account_id = ba.id
        session.flush()

    # Update profile
    billing_address_dict = None
    if profile_update.billing_address is not None:
        billing_address_dict = profile_update.billing_address.model_dump(
            exclude_none=True,
        )

        # Validate billing address fields
        from orchestra.web.api.utils.business_validation import (
            validate_billing_address_data,
        )

        if (
            billing_address_dict.get("line1")
            or billing_address_dict.get(
                "city",
            )
            or billing_address_dict.get("country")
        ):
            is_valid, error_msg = validate_billing_address_data(
                line1=billing_address_dict.get("line1"),
                city=billing_address_dict.get("city"),
                country=billing_address_dict.get("country"),
                line2=billing_address_dict.get("line2"),
                state=billing_address_dict.get("state"),
                postal_code=billing_address_dict.get("postal_code"),
            )
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid billing address: {error_msg}",
                )

    # Validate tax_id if provided along with country
    existing_billing_address = ba.billing_address
    if profile_update.tax_id is not None:
        # Get country from billing_address (either new or existing)
        country = None
        if billing_address_dict and billing_address_dict.get("country"):
            country = billing_address_dict["country"]
        elif existing_billing_address and existing_billing_address.get("country"):
            country = existing_billing_address["country"]

        if country:
            is_valid, formatted_id, error = TaxIDValidator.validate_tax_id(
                profile_update.tax_id,
                country,
            )
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid tax ID for {country}: {error}",
                )
            # Use the formatted version if validation succeeded
            profile_update.tax_id = formatted_id

    # Update profile fields directly on BillingAccount
    if profile_update.billing_email is not None:
        ba.billing_email = profile_update.billing_email
    if profile_update.business_name is not None:
        ba.name = profile_update.business_name
    if profile_update.tax_id is not None:
        ba.tax_id = profile_update.tax_id
    if billing_address_dict is not None:
        ba.billing_address = billing_address_dict
    session.commit()

    # Sync changes to Stripe if org has a Stripe customer via BillingAccount
    if ba.stripe_customer_id:
        sync_billing_profile_to_stripe(
            ba.stripe_customer_id,
            is_business=True,
            billing_email=profile_update.billing_email,
            name=profile_update.business_name,
            tax_id=profile_update.tax_id,
            billing_address=billing_address_dict,
            existing_billing_address=existing_billing_address,
            logger_instance=logger,
        )

    # Return updated profile from the BillingAccount directly
    session.refresh(ba)
    return OrganizationBusinessProfileResponse(
        billing_email=ba.billing_email,
        business_name=ba.name,
        tax_id=ba.tax_id,
        billing_address=ba.billing_address,
    ).model_dump()


# ============================================================================
# Organization Stripe Customer Endpoints (moved from organization/views.py)
# ============================================================================


@router.post(
    "/organizations/{organization_id}/billing/stripe-customer",
    tags=["organization-billing"],
    response_model=dict,
)
async def ensure_organization_stripe_customer(
    request_fastapi: Request,
    organization_id: int,
    body: Optional[OrganizationStripeCustomerCreateRequest] = Body(default=None),
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Ensure a Stripe customer exists for an organization.

    This endpoint creates a Stripe customer for the organization if one doesn't
    exist, or returns the existing customer ID. This enables direct billing
    for the organization.

    Requires billing:write permission (Owners and Admins).

    The organization must have a billing_email set (either in business profile
    or provided in the request body) for Stripe customer creation.

    Returns:
        - organization_id: The organization's ID
        - stripe_customer_id: The Stripe customer ID
        - is_new: True if the customer was just created, False if it existed
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:write permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage billing for this organization",
        )

    # Get billing account (created eagerly in organization_dao.create;
    # fallback handles legacy orgs that may not have one yet)
    billing_account_dao = BillingAccountDAO(session)
    ba = org.billing_account
    if ba is None:
        ba = billing_account_dao.create()
        org.billing_account_id = ba.id
        session.flush()

    # If Stripe customer already exists, return it
    if ba.stripe_customer_id:
        return OrganizationStripeCustomerResponse(
            organization_id=organization_id,
            stripe_customer_id=ba.stripe_customer_id,
            is_new=False,
        ).model_dump()

    # Determine email for Stripe customer
    billing_email = None
    if body and body.billing_email:
        billing_email = body.billing_email
    elif ba.billing_email:
        billing_email = ba.billing_email
    else:
        # Fall back to owner's email
        user_dao = UserDAO(session)
        owner = user_dao.get_by_id(org.owner_id)
        if owner:
            billing_email = owner[0].email

    if not billing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization must have a billing_email set or provide one in the request",
        )

    # Determine name for Stripe customer
    business_name = None
    if body and body.business_name:
        business_name = body.business_name
    elif ba.name:
        business_name = ba.name
    else:
        business_name = org.name  # Fall back to org name

    # Configure Stripe
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe is not configured",
        )

    stripe.api_key = settings.stripe_secret_key

    try:
        # Build Stripe customer params including address and tax ID if available
        from orchestra.web.api.utils.business_validation import (
            build_stripe_customer_name,
            get_stripe_tax_exempt_status,
            get_stripe_tax_id_data,
        )

        customer_params = {
            "email": billing_email,
            **build_stripe_customer_name(is_business=True, name=business_name),
            "metadata": {
                "organization_id": str(organization_id),
                "organization_name": org.name,
                "billing_account_id": str(ba.id),
            },
        }

        # Sync billing address to Stripe if available (from BillingAccount)
        ba_address = ba.billing_address or {}
        if ba_address.get("line1"):
            customer_params["address"] = {
                "line1": ba_address.get("line1", ""),
                "line2": ba_address.get("line2", ""),
                "city": ba_address.get("city", ""),
                "state": ba_address.get("state", ""),
                "postal_code": ba_address.get("postal_code", ""),
                "country": ba_address.get("country", ""),
            }
            # Validate location immediately for tax calculations
            customer_params["tax"] = {"validate_location": "immediately"}

        # Sync tax ID to Stripe if available
        country_code = ba_address.get("country")
        tax_id_data = get_stripe_tax_id_data(ba.tax_id, country_code)
        if tax_id_data:
            customer_params["tax_id_data"] = tax_id_data

        # Set tax_exempt based on B2B tax ID status
        customer_params["tax_exempt"] = get_stripe_tax_exempt_status(
            ba.tax_id,
            country_code,
        )

        # Create Stripe customer
        customer = stripe.Customer.create(**customer_params)

        # Store the Stripe customer ID on the BillingAccount
        ba.stripe_customer_id = customer.id

        # Update business profile if provided in request
        if body:
            if body.billing_email and body.billing_email != ba.billing_email:
                ba.billing_email = body.billing_email
            if body.business_name and body.business_name != ba.name:
                ba.name = body.business_name

        session.commit()

        return OrganizationStripeCustomerResponse(
            organization_id=organization_id,
            stripe_customer_id=customer.id,
            is_new=True,
        ).model_dump()

    except stripe.error.StripeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create Stripe customer: {str(e)}",
        )


@router.get(
    "/organizations/{organization_id}/billing/stripe-customer",
    tags=["organization-billing"],
)
async def get_organization_stripe_customer(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get the Stripe customer ID for an organization.

    Returns the Stripe customer ID if one exists, or indicates if direct
    billing is not yet set up.

    Requires billing:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:read permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view billing info for this organization",
        )

    ba = org.billing_account
    stripe_cust_id = ba.stripe_customer_id if ba else None
    if not stripe_cust_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization does not have direct billing set up. "
            "Use POST to create a Stripe customer.",
        )

    return OrganizationStripeCustomerResponse(
        organization_id=organization_id,
        stripe_customer_id=stripe_cust_id,
        is_new=False,
    ).model_dump()
