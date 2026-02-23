"""Organization management schemas."""

from datetime import datetime
from typing import Dict, Optional
from zoneinfo import available_timezones

from pydantic import BaseModel, Field, field_validator

VALID_TIMEZONES = available_timezones()


class OrganizationCreate(BaseModel):
    """Schema for creating an organization."""

    name: str
    timezone: Optional[str] = (
        None  # IANA timezone; defaults to owner's timezone if not set
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        """Ensure the timezone is a valid IANA timezone name."""
        if v is None:
            return v
        if v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v


class OrganizationUpdate(BaseModel):
    """Schema for updating an organization."""

    name: Optional[str] = None
    timezone: Optional[str] = None  # IANA timezone (e.g., "America/New_York")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        """Ensure the timezone is a valid IANA timezone name."""
        if v is None:
            return v
        if v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v


class OrganizationOwnershipTransfer(BaseModel):
    """Schema for transferring organization ownership."""

    new_owner_id: str


class OrganizationResponse(BaseModel):
    """Schema for organization response."""

    id: int
    name: str
    owner_id: str
    timezone: Optional[str] = None  # IANA timezone (e.g., "America/New_York")
    created_at: datetime

    model_config = {"from_attributes": True}


class OrganizationMemberAdd(BaseModel):
    """Schema for adding a member to an organization."""

    user_id: str
    role_id: Optional[int] = None  # RBAC role (defaults to Member role if not provided)


class OrganizationMemberRemove(BaseModel):
    """Schema for removing a member from an organization."""

    user_id: str


class OrganizationMemberRoleUpdate(BaseModel):
    """Schema for updating a member's role."""

    role_id: int


class OrganizationMemberResponse(BaseModel):
    """Schema for organization member response."""

    id: int
    user_id: str
    organization_id: int
    role_id: int
    role_name: Optional[str] = None
    created_at: datetime
    # User info fields (populated from User)
    name: Optional[str] = None
    email: Optional[str] = None
    image: Optional[str] = None
    bio: Optional[str] = None
    timezone: Optional[str] = None
    phone_number: Optional[str] = None

    model_config = {"from_attributes": True}


# ============== Organization Invite Schemas ==============


class InviteUserRequest(BaseModel):
    """Schema for inviting a user to an organization."""

    email: str
    role_id: Optional[int] = None  # Defaults to Member role if not provided
    expires_in_days: int = 7  # Default 7 days


class InviteResponse(BaseModel):
    """Schema for organization invite response.

    All invites in the system are pending - they are deleted when accepted/declined.
    """

    id: str
    token: str
    organization_id: int
    organization_name: str
    invitee_email: str
    invited_by_user_id: str
    invited_by_name: Optional[str] = None
    role_id: int
    role_name: Optional[str] = None
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class InviteListResponse(BaseModel):
    """Schema for listing organization invites."""

    invites: list[InviteResponse]


class AcceptInviteResponse(BaseModel):
    """Schema for accepting an invite."""

    message: str
    organization_id: int
    organization_name: str
    api_key: str


class DeclineInviteResponse(BaseModel):
    """Schema for declining an invite."""

    message: str


# ============== Organization Billing Schemas ==============


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
    The `country` field is required for tax calculations.
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


class OrganizationCheckoutRequest(BaseModel):
    """Schema for creating a checkout session."""

    amount: int  # Amount in credits (1 credit = $1)
    success_url: str
    cancel_url: str


class OrganizationCheckoutResponse(BaseModel):
    """Schema for checkout session response."""

    checkout_url: str
    session_id: str


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


# ============================================================================
# Spending Limit Schemas
# ============================================================================


class OrgSpendingLimitRequest(BaseModel):
    """Request body for setting organization spending limit."""

    monthly_spending_cap: Optional[float] = Field(
        ...,
        description="Monthly spending limit in dollars. Set to null to remove the limit.",
        example=500.00,
        ge=0,
    )


class OrgSpendingLimitResponse(BaseModel):
    """Response for setting organization spending limit."""

    organization_id: int = Field(..., description="Organization ID.")
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="The set monthly spending limit.",
        example=500.00,
    )
    cascaded_updates: Optional[Dict[str, int]] = Field(
        None,
        description="Count of child entities that had their limits capped.",
        example={"users_capped": 3, "assistants_capped": 7},
    )


# ============================================================================
# Member Spending Limit Schemas
# ============================================================================


class MemberSpendingLimitRequest(BaseModel):
    """Request body for setting a member's spending limit within an org."""

    monthly_spending_cap: Optional[float] = Field(
        ...,
        description="Monthly spending limit in dollars for this member. Set to null for no limit.",
        example=100.00,
        ge=0,
    )


class MemberSpendingLimitResponse(BaseModel):
    """Response for setting a member's spending limit."""

    organization_id: int = Field(..., description="Organization ID.")
    user_id: str = Field(..., description="User ID.")
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="The set monthly spending limit for this member.",
        example=100.00,
    )
    assistants_capped: int = Field(
        0,
        description="Number of assistants that had their limits reduced due to this change.",
    )


class OrgSpendResponse(BaseModel):
    """Response for getting organization's cumulative spend."""

    organization_id: int = Field(..., description="Organization ID.")
    month: str = Field(description="Month in YYYY-MM format.")
    cumulative_spend: float = Field(description="Cumulative spend for the month.")
    limit: Optional[float] = Field(
        None,
        description="Monthly spending limit for the org.",
    )
    limit_set_at: Optional[datetime] = Field(
        None,
        description="When the spending limit was last changed.",
    )
    percent_used: Optional[float] = Field(None, description="Percentage of limit used.")
    credit_balance: Optional[float] = Field(
        None,
        description="Current credit balance of the billing account.",
    )


class MemberSpendResponse(BaseModel):
    """Response for getting an organization member's cumulative spend."""

    organization_id: int = Field(..., description="Organization ID.")
    user_id: str = Field(..., description="User ID of the member.")
    month: str = Field(description="Month in YYYY-MM format.")
    cumulative_spend: float = Field(
        description="Cumulative spend for the member in this org.",
    )
    limit: Optional[float] = Field(
        None,
        description="Member's spending limit in this org.",
    )
    limit_set_at: Optional[datetime] = Field(
        None,
        description="When the spending limit was last changed.",
    )
    percent_used: Optional[float] = Field(None, description="Percentage of limit used.")
    credit_balance: Optional[float] = Field(
        None,
        description="Current credit balance of the billing account.",
    )
