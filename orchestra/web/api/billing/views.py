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
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException, Request, status
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
    AvailablePlanItem,
    AvailablePlansResponse,
    BillingProfileResponse,
    BillingProfileUpdate,
    CheckoutSessionResponse,
    CheckoutStatusResponse,
    CurrentPeriodUsageResponse,
    CurrentPlanSummary,
    InvoiceListItem,
    InvoiceListResponse,
    InvoiceUrlsResponse,
    PortalSessionResponse,
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


def _customer_has_payment_method(stripe_customer_id: Optional[str]) -> bool:
    """
    Check whether a Stripe customer has a default payment method on file.

    Returns ``False`` when there is no customer or Stripe is not configured.
    """
    if not stripe_customer_id:
        return False
    try:
        _init_stripe()
        customer = stripe.Customer.retrieve(
            stripe_customer_id,
            expand=["invoice_settings.default_payment_method"],
        )
        # Check invoice_settings.default_payment_method first (used for invoices),
        # then fall back to the legacy default_source field.
        if (
            customer.invoice_settings
            and customer.invoice_settings.default_payment_method
        ):
            return True
        if customer.default_source:
            return True
        return False
    except Exception:
        # If Stripe is unreachable, don't block the response – just
        # report "no payment method" so the UI shows the right hint.
        logger.warning(
            "Could not verify payment method for customer %s",
            stripe_customer_id,
            exc_info=True,
        )
        return False


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

    # Resolve the effective plan via the DAO (every account has an
    # active assignment from signup — the default plan — so the
    # call always returns a real plan) and project Decimals onto the
    # JSON-friendly response schema. ``billing_mode`` is also surfaced
    # at the top level so the frontend can branch without drilling
    # into the nested ``plan`` object.
    from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO

    plan = BillingPlanAssignmentDAO(session).resolve_effective_plan(ba.id)
    plan_summary = CurrentPlanSummary.from_effective_plan(plan)

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
        billing_mode=plan.billing_mode,
        plan=plan_summary,
        plan_group_id=ba.plan_group_id,
    )


# ============================================================================
# POST /billing/checkout-session
# ============================================================================


@router.post(
    "/billing/checkout-session",
    response_model=CheckoutSessionResponse,
    include_in_schema=False,
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

    # METERED accounts pay by monthly invoice (handled by
    # ``monthly_metered_invoicer``) — they don't top up a credits
    # wallet. Block the Buy-Credits flow rather than letting the
    # checkout succeed and silently grant credits that would never get
    # debited (METERED's deduct_credits doesn't touch the wallet).
    from orchestra.db.models.orchestra_models import BillingMode

    if ba_dao.resolve_billing_mode(ba) == BillingMode.METERED:
        raise HTTPException(
            status_code=400,
            detail=(
                "Account is on a METERED billing plan; usage is invoiced "
                "monthly. Buying credits is disabled."
            ),
        )

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
            logger.error(
                f"Stripe checkout session creation failed: {exc}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=400,
                detail="Failed to create checkout session",
            )

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
        logger.error(f"Stripe portal session creation failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail="Failed to create billing portal session",
        )

    return PortalSessionResponse(url=portal_session.url)


# ============================================================================
# GET /billing/checkout-status
# ============================================================================


@router.get(
    "/billing/checkout-status",
    response_model=CheckoutStatusResponse,
    include_in_schema=False,
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
        logger.error(f"Stripe checkout session retrieval failed: {exc}", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid checkout session")

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
    has_pm = _customer_has_payment_method(ba.stripe_customer_id)

    blocked_reason = None
    if not ba.autorecharge:
        if ba.account_status in ("SUSPENDED", "CLOSED"):
            blocked_reason = "account_status"
        elif ba_dao.has_unpaid_auto_recharges(ba.id):
            blocked_reason = "unpaid_invoice"
        elif not can_enable:
            blocked_reason = "spending"
        elif not has_pm:
            blocked_reason = "payment_method"

    return AutoRechargeResponse(
        enabled=ba.autorecharge,
        threshold=float(ba.autorecharge_threshold),
        qty=float(ba.autorecharge_qty),
        min_recharge_amount=float(MIN_AUTORECHARGE_AMOUNT),
        eligible=can_enable,
        total_spending=total_spending,
        minimum_spend_required=min_required,
        remaining_spend_needed=max(0.0, min_required - total_spending),
        has_payment_method=has_pm,
        blocked_reason=blocked_reason,
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
        if ba.account_status in ("SUSPENDED", "CLOSED"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Auto-recharge cannot be enabled while your account "
                    f"is {ba.account_status.lower()}. "
                    "Please contact support."
                ),
            )
        if ba_dao.has_unpaid_auto_recharges(ba.id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Auto-recharge cannot be enabled while you have an "
                    "outstanding unpaid invoice. It will be available "
                    "once your invoice is paid."
                ),
            )
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
        if not _customer_has_payment_method(ba.stripe_customer_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "A default payment method is required to enable "
                    "auto-recharge. Please add one via Manage Payment Methods."
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

    # --- Apply updates via DAO -------------------------------------------
    ba_dao.set_autorecharge(ba.id, body.enabled)

    if body.threshold is not None:
        ba_dao.set_autorecharge_threshold(ba.id, body.threshold)

    if body.qty is not None:
        ba_dao.set_autorecharge_qty(ba.id, body.qty)

    session.commit()
    session.refresh(ba)

    # --- Return updated state + eligibility ------------------------------
    total_spending = float(ba_dao.get_total_spending(ba.id))
    can_enable = ba_dao.can_enable_auto_recharge(ba.id)
    min_required = float(MIN_SPEND_FOR_AUTO_RECHARGE)
    has_pm = _customer_has_payment_method(ba.stripe_customer_id)

    return AutoRechargeResponse(
        enabled=ba.autorecharge,
        threshold=float(ba.autorecharge_threshold),
        qty=float(ba.autorecharge_qty),
        min_recharge_amount=float(MIN_AUTORECHARGE_AMOUNT),
        eligible=can_enable,
        total_spending=total_spending,
        minimum_spend_required=min_required,
        remaining_spend_needed=max(0.0, min_required - total_spending),
        has_payment_method=has_pm,
    )


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

    ba_dao = BillingAccountDAO(session)
    ba = ba_dao.resolve(user_id, organization_id)

    is_business = organization_id is not None

    if not ba:
        return BillingProfileResponse(is_business=is_business)

    profile = ba_dao.get_billing_profile(ba.id)
    if not profile:
        return BillingProfileResponse(is_business=is_business)

    return BillingProfileResponse(
        billing_email=profile.get("billing_email"),
        name=profile.get("name"),
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
    resolved_name = profile_update.name
    tax_id = profile_update.tax_id
    tax_id_type = profile_update.tax_id_type
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

    return BillingProfileResponse(
        billing_email=profile.get("billing_email") if profile else None,
        name=profile.get("name") if profile else None,
        tax_id=profile.get("tax_id") if profile else None,
        tax_id_type=profile.get("tax_id_type") if profile else None,
        billing_address=profile.get("billing_address", {}) if profile else {},
        billing_setup_complete=(
            profile.get("billing_setup_complete", False) if profile else False
        ),
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
    from orchestra.routines.monthly_metered_invoicer import (
        estimate_in_progress_invoice,
    )

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
    from orchestra.db.dao.billing_plan_assignment_dao import (
        BillingPlanAssignmentDAO,
        next_month_boundary_utc,
    )
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
    from orchestra.db.models.orchestra_models import BillingPlanTemplate
    from sqlalchemy import select as _select

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
    from orchestra.db.models.orchestra_models import (
        BillingMode,
        BillingPlanTemplate,
    )
    from orchestra.lib.billing import ensure_stripe_customer

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
                is_business=organization_id is not None,
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
                    "An auto-recharge or top-up is still being invoiced "
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
