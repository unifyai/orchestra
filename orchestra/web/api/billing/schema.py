"""Pydantic schemas for billing endpoints."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

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

    # Whether the Stripe customer has a default payment method on file
    has_payment_method: bool = False

    # If non-null, auto-recharge cannot be enabled and this explains why.
    # Possible values:
    #   "unpaid_invoice" – outstanding auto-recharge invoice being retried
    #   "account_status" – account is SUSPENDED / CLOSED
    #   "spending"       – spending threshold not met
    #   "payment_method" – no default payment method
    blocked_reason: Optional[str] = None


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


class BillingAddress(BaseModel):
    """Structured billing address — only known fields are accepted."""

    model_config = ConfigDict(extra="forbid")

    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class BillingProfileUpdate(BaseModel):
    """
    Unified billing profile update for both personal and org contexts.

    Accepted by ``PATCH /billing/billing-profile``.
    """

    billing_email: Optional[str] = None
    name: Optional[str] = None
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Optional[BillingAddress] = None


# ---------------------------------------------------------------------------
# Tax Validation Schemas
# ---------------------------------------------------------------------------


class TaxIdValidationRequest(BaseModel):
    """Request body for ``POST /billing/validate-tax-id``."""

    tax_id: str = Field(..., description="Tax ID to validate")
    country: str = Field(..., description="Two-letter country code")
