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
    image: Optional[str] = None
    timezone: Optional[str] = None  # IANA timezone (e.g., "America/New_York")
    free_trial: bool = False
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
    mfa_setup_required: bool = False


class DeclineInviteResponse(BaseModel):
    """Schema for declining an invite."""

    message: str


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


# ============================================================================
# Organization MFA Enforcement Schemas
# ============================================================================


class OrgMFASettingsRequest(BaseModel):
    """Request body for updating organization MFA enforcement settings."""

    require_mfa: bool = Field(
        ...,
        description="Whether to require MFA for all members.",
    )


class OrgMFASettingsResponse(BaseModel):
    """Response for organization MFA enforcement settings."""

    require_mfa: bool = Field(
        ...,
        description="Whether MFA is required for all members.",
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
