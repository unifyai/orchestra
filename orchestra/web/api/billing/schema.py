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
# User Billing Profile Schemas (moved from users/schema.py)
# ---------------------------------------------------------------------------


class UserBillingProfileUpdate(BaseModel):
    """Schema for updating user billing profile.

    Accepts ``individual_name`` (preferred) or ``business_name``
    (backward-compat alias).  If both are provided, ``individual_name``
    takes precedence.
    """

    billing_email: Optional[str] = None
    individual_name: Optional[str] = None
    business_name: Optional[str] = None  # backward-compat alias
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Optional[Dict[str, Any]] = None

    @property
    def resolved_name(self) -> Optional[str]:
        """Return the effective name (individual_name wins)."""
        return self.individual_name or self.business_name


class UserBillingProfileResponse(BaseModel):
    """Schema for user billing profile response."""

    billing_email: Optional[str] = None
    individual_name: Optional[str] = None
    business_name: Optional[str] = None  # backward-compat alias (same value)
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Dict[str, Any] = Field(default_factory=dict)
    billing_setup_complete: bool = False


# ---------------------------------------------------------------------------
# Organization Billing Schemas (moved from organization/schema.py)
# ---------------------------------------------------------------------------


class OrganizationBillingResponse(BaseModel):
    """Schema for organization billing information."""

    organization_id: int
    organization_name: str
    credits: float
    stripe_customer_id: Optional[str] = None  # Set when billing is set up
    autorecharge: bool
    autorecharge_threshold: float
    autorecharge_qty: float
    account_status: str
    billing_setup_complete: bool

    model_config = {"from_attributes": True}


class OrganizationBillingUpdate(BaseModel):
    """Schema for updating organization billing settings."""

    autorecharge: Optional[bool] = None
    autorecharge_threshold: Optional[float] = None
    autorecharge_qty: Optional[float] = None


class BillingAddress(BaseModel):
    """
    Flexible international billing address.

    Supports various address formats worldwide.
    The ``country`` field is required for tax calculations.
    Other fields are optional to accommodate different country formats.
    """

    country: str  # Required: ISO 3166-1 alpha-2 (e.g., "US", "IN", "GB")
    formatted: Optional[str] = None  # Full formatted address for display
    line1: Optional[str] = None  # Primary street address
    line2: Optional[str] = None  # Secondary address (apt, suite, etc.)
    city: Optional[str] = None
    state: Optional[str] = None  # State, province, or region
    postal_code: Optional[str] = None  # ZIP code, PIN code, postcode
    locality: Optional[str] = None  # For countries that use locality
    district: Optional[str] = None  # For countries like India
    sublocality: Optional[str] = None  # Neighborhood, ward, etc.

    model_config = {"extra": "allow"}  # Allow additional country-specific fields


class OrganizationBusinessProfileUpdate(BaseModel):
    """Schema for updating organization business profile."""

    billing_email: Optional[str] = None
    business_name: Optional[str] = None
    tax_id: Optional[str] = None
    billing_address: Optional[BillingAddress] = None


class OrganizationBusinessProfileResponse(BaseModel):
    """Schema for organization business profile."""

    billing_email: Optional[str] = None
    business_name: Optional[str] = None
    tax_id: Optional[str] = None
    billing_address: Optional[dict] = None  # Flexible dict for any address format


class OrganizationCreditsResponse(BaseModel):
    """Schema for organization credits balance."""

    organization_id: int
    credits: float


class OrganizationStripeCustomerResponse(BaseModel):
    """Schema for organization Stripe customer info."""

    organization_id: int
    stripe_customer_id: str
    is_new: bool = False  # True if customer was just created


class OrganizationStripeCustomerCreateRequest(BaseModel):
    """Schema for creating/ensuring organization Stripe customer."""

    # Optional business info to set during creation
    business_name: Optional[str] = None
    billing_email: Optional[str] = None


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
    name: Optional[str] = None  # canonical name field
    individual_name: Optional[str] = None  # backward-compat alias for personal
    business_name: Optional[str] = None  # backward-compat alias for org
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Dict[str, Any] = Field(default_factory=dict)
    billing_setup_complete: bool = False
    is_business: bool = False


class BillingProfileUpdate(BaseModel):
    """
    Unified billing profile update for both personal and org contexts.

    Accepted by ``PATCH /billing/billing-profile``.
    Accepts ``name`` (preferred), ``individual_name``, or ``business_name``
    (backward-compat aliases).  If multiple are provided, ``name`` wins,
    then ``individual_name``, then ``business_name``.
    """

    billing_email: Optional[str] = None
    name: Optional[str] = None
    individual_name: Optional[str] = None  # backward-compat alias
    business_name: Optional[str] = None  # backward-compat alias
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    billing_address: Optional[Dict[str, Any]] = None

    @property
    def resolved_name(self) -> Optional[str]:
        """Return the effective name (name > individual_name > business_name)."""
        return self.name or self.individual_name or self.business_name
