"""Organization management schemas."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class OrganizationCreate(BaseModel):
    """Schema for creating an organization."""

    name: str
    # Note: billing_user_id is always set to owner_id automatically


class OrganizationUpdate(BaseModel):
    """Schema for updating an organization."""

    name: Optional[str] = None
    # Note: billing_user_id cannot be updated directly.
    # Use the transfer-ownership endpoint to change both owner and billing user.


class OrganizationOwnershipTransfer(BaseModel):
    """Schema for transferring organization ownership."""

    new_owner_id: str


class OrganizationResponse(BaseModel):
    """Schema for organization response."""

    id: int
    name: str
    owner_id: str
    billing_user_id: str
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
    # User info fields (populated from AuthUser)
    name: Optional[str] = None
    email: Optional[str] = None
    image: Optional[str] = None

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
    billing_mode: str  # "delegated" or "direct"
    credits: float
    billing_user_id: Optional[str] = None  # Only for delegated billing
    stripe_customer_id: Optional[str] = None  # Only for direct billing
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
