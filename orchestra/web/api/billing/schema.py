"""Pydantic schemas for billing endpoints."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Checkout / Portal / Status (original billing schemas)
# ---------------------------------------------------------------------------


class CheckoutSessionResponse(BaseModel):
    """Response from the checkout-session endpoint."""

    url: str
    session_id: str


class PortalSessionResponse(BaseModel):
    """Response from the portal-session endpoint."""

    url: str


class CheckoutStatusResponse(BaseModel):
    """Response from the checkout-status endpoint."""

    status: Optional[str] = None
    payment_status: Optional[str] = None


class AutoRechargeResponse(BaseModel):
    """
    Combined auto-recharge settings and eligibility.

    Returned by ``GET /billing/auto-recharge``.
    """

    # Current settings
    enabled: bool = False
    threshold: float = 0.0
    qty: float = 25.0

    # Validation constraints (so frontends don't hardcode them)
    min_recharge_amount: float = 25.0

    # Eligibility (fraud-prevention spending gate)
    eligible: bool = False
    total_spending: float = 0.0
    minimum_spend_required: float = 0.0
    remaining_spend_needed: float = 0.0


class AutoRechargeUpdateRequest(BaseModel):
    """
    Request body for ``PUT /billing/auto-recharge``.

    Only ``enabled`` is required.  ``threshold`` and ``qty`` are optional
    so callers can toggle the feature on/off without re-sending the amounts.
    """

    enabled: bool
    threshold: Optional[float] = None
    qty: Optional[float] = None


class AccountInfoResponse(BaseModel):
    """
    Response from ``GET /billing/account-info``.

    Returns the key billing account fields needed by the frontend:
    credit balance, billing history indicator, auto-recharge settings,
    and account status.  Context (personal vs org) is derived from
    the API key.
    """

    billing_account_id: int
    credits: float = 0.0
    account_status: str = "ACTIVE"
    last_recharge_at: Optional[str] = None

    # Auto-recharge settings (mirrors AutoRechargeResponse subset)
    autorecharge: bool = False
    autorecharge_threshold: float = 0.0
    autorecharge_qty: float = 25.0


# ---------------------------------------------------------------------------
# Unified Billing Profile Schemas
# ---------------------------------------------------------------------------


class BillingProfileResponse(BaseModel):
    """
    Unified billing profile response for both personal and org contexts.

    Returned by ``GET /billing/billing-profile``.
    Context (personal vs org) is derived from the API key.
    """

    billing_email: Optional[str] = None
    name: Optional[str] = None
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Dict[str, Any] = Field(default_factory=dict)
    billing_setup_complete: bool = False
    is_business: bool = False


class BillingProfileUpdate(BaseModel):
    """
    Unified billing profile update for both personal and org contexts.

    Accepted by ``PATCH /billing/billing-profile``.
    """

    billing_email: Optional[str] = None
    name: Optional[str] = None
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Optional[Dict[str, Any]] = None
