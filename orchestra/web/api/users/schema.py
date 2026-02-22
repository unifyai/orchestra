from datetime import datetime
from typing import Any, Dict, Literal, Optional
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VALID_TIMEZONES = available_timezones()


class UserRequest(BaseModel):
    email: Optional[str] = None
    user_id: Optional[str] = None
    image: Optional[str] = None
    name: Optional[str] = None
    last_name: Optional[str] = None
    job_title: Optional[str] = None
    bio: Optional[str] = None
    timezone: Optional[str] = None
    phone_number: Optional[str] = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        """Ensure the timezone is a valid IANA timezone name."""
        if v is None:
            # Allow timezone to be optional
            return v
        if v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, v: Optional[str]) -> Optional[str]:
        """Validate phone number and normalize to E.164 format."""
        if v is None:
            return v
        from orchestra.web.api.utils.phone_number_validator import validate_phone_number

        result = validate_phone_number(v)
        if not result["is_valid"]:
            raise ValueError(f"Invalid phone number: {result['error']}")
        return result["formatted_phone_number"]


class AccountRequest(BaseModel):
    provider: str
    type: str
    provider_account_id: str
    access_token: str
    expires_at: int
    scope: str
    token_type: str
    id_token: str
    user_id: Optional[str] = None


class FreezeAccountRequest(BaseModel):
    user_id: str
    freeze: bool = True


class FreezeAccountByStripeIdRequest(BaseModel):
    stripe_id: str
    freeze: bool


class StripeIdRequest(BaseModel):
    user_id: str
    stripe_id: str


class QueryLoggingStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool


class UpdateQueryLoggingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool


# -- Credit grant links --
class CreditGrantClaimResponse(BaseModel):
    """Response for claiming a one-time credit grant link."""

    message: str
    credits_granted: Optional[float] = None


class CreditGrantLinkClaimRequest(BaseModel):
    """Request to claim a one-time credit grant link."""

    token: str


class CreditGrantLinkCreateRequest(BaseModel):
    """Request to create a one-time credit grant link."""

    expires_in_days: int = 7
    credit_amount: Optional[float] = None  # Defaults to assistant_creation_cost


class CreditGrantLinkResponse(BaseModel):
    """Response containing one-time credit grant link details."""

    id: str
    token: str
    expires_at: datetime
    claimed_at: Optional[datetime] = None
    user_id: Optional[str] = None
    claimed_by_email: Optional[str] = None
    credit_amount: float  # Amount of credits granted when claimed


class OnboardingStatusResponse(BaseModel):
    """Response containing user's onboarding status."""

    onboarded: bool


class UpdateOnboardingStatusRequest(BaseModel):
    """Request to update user's onboarding status."""

    onboarded: bool


# -- Account Deletion --


class AccountDeletionConfirmation(BaseModel):
    """Confirmation required for account deletion."""

    confirm_email: str


class DeletionBlockerResponse(BaseModel):
    """Details about why account deletion is blocked."""

    reason: Literal["pending_bills", "organization_owner", "user_not_found"]
    details: dict


class CanDeleteAccountResponse(BaseModel):
    """Response for pre-flight deletion check."""

    can_delete: bool
    blockers: list[DeletionBlockerResponse]


class AccountDeletionResponse(BaseModel):
    """Response after account deletion attempt."""

    success: bool
    message: str


# ============================================================================
# User Spending Limit Schemas (Personal Context)
# ============================================================================


class UserSpendingLimitRequest(BaseModel):
    """Request body for setting user's personal spending limit."""

    monthly_spending_cap: Optional[float] = Field(
        ...,
        description="Monthly spending limit in dollars for personal usage. Set to null for no limit.",
        example=200.00,
        ge=0,
    )


class UserSpendingLimitResponse(BaseModel):
    """Response for user's personal spending limit."""

    user_id: str = Field(..., description="User ID.")
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="The monthly spending limit for personal usage.",
        example=200.00,
    )
    assistants_capped: int = Field(
        0,
        description="Number of personal assistants that had their limits reduced.",
    )


class UserSpendResponse(BaseModel):
    """Response for user's current spend."""

    user_id: str = Field(..., description="User ID.")
    month: str = Field(..., description="Month in YYYY-MM format.")
    cumulative_spend: float = Field(..., description="Cumulative spend for the month.")
    limit: Optional[float] = Field(
        None,
        description="Monthly spending limit for the user.",
    )
    limit_set_at: Optional[datetime] = Field(
        None,
        description="When the spending limit was last changed.",
    )
    percent_used: Optional[float] = Field(
        None,
        description="Percentage of limit used (null if no limit).",
    )
    credit_balance: Optional[float] = Field(
        None,
        description="Current credit balance of the billing account.",
    )


# ============================================================================
# Onboarding Status Schemas
# ============================================================================


# Business address used for billing/onboarding.
# Field names match Stripe address format and BillingAccount.billing_address JSONB keys.
class BusinessAddress(BaseModel):
    """Address information for billing purposes."""

    line1: str
    line2: Optional[str] = None
    city: str
    state: Optional[str] = None
    country: str
    postal_code: Optional[str] = None


# Valid onboarding steps (enforced in schema, freeform in DB)
# The step represents WHERE TO RESUME, not where the user currently is.
# - account_setup: User needs to complete account setup (initial state)
# - billing_setup: Account setup done, user needs to complete billing
# - completed: All onboarding steps done
OnboardingStep = Literal[
    "account_setup",  # Initial state - user needs to set up account (personal/business choice)
    "billing_setup",  # Account done - user needs to add payment method
    "completed",  # All done
]


class OnboardingStepDataResponse(BaseModel):
    """
    Accumulated step data from onboarding progress.

    Data is accumulated as user progresses through steps.
    All fields are optional since they're filled in progressively.
    """

    model_config = ConfigDict(extra="allow")

    # Account setup data (filled when account_setup is completed)
    selected_type: Optional[Literal["personal", "business"]] = None

    # Organization data (filled if selected_type is "business")
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None
    business_name: Optional[str] = None
    tax_id: Optional[str] = None
    billing_address: Optional[BusinessAddress] = None

    # Billing setup data (filled when billing_setup is completed)
    billing_skipped: Optional[bool] = None
    payment_method_added: Optional[bool] = None

    # Completion data
    completed_at: Optional[str] = None  # ISO datetime string


class OnboardingStatusDetailedResponse(BaseModel):
    """Detailed response for user's onboarding status."""

    user_id: str
    current_step: OnboardingStep
    step_data: OnboardingStepDataResponse
    created_at: datetime
    updated_at: Optional[datetime] = None


class OnboardingStatusUpdateRequest(BaseModel):
    """
    Request to update user's onboarding status.

    The current_step indicates WHERE TO RESUME next time:
    - After completing account setup, set to "billing_setup"
    - After completing billing setup, set to "completed"

    The step_data accumulates information from all completed steps.
    """

    current_step: OnboardingStep
    step_data: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_step_data(self):
        """Validate step_data contains valid fields."""
        if self.step_data is None:
            return self

        # Validate that step_data can be parsed as OnboardingStepDataResponse
        # This ensures only valid fields are stored
        OnboardingStepDataResponse(**self.step_data)
        return self


class OnboardingStatusCreateRequest(BaseModel):
    """Request to create onboarding status (internal/admin use)."""

    user_id: str
    current_step: OnboardingStep = "account_setup"
    step_data: Optional[Dict[str, Any]] = None


# ============================================================================
# User Billing / Checkout Schemas
# ============================================================================


class UserCheckoutRequest(BaseModel):
    """
    Request model for creating a Stripe checkout session for user credits.

    Attributes:
        amount (int): Amount of credits to purchase (1 credit = $1, minimum 5, max 10000).
        success_url (str): URL to redirect to on successful payment.
        cancel_url (str): URL to redirect to on cancelled payment.
    """

    amount: int
    success_url: str
    cancel_url: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_valid(cls, v: int) -> int:
        if v < 5:
            raise ValueError("Minimum purchase amount is 5 credits ($5)")
        if v > 10000:
            raise ValueError("Maximum purchase amount is 10000 credits ($10,000)")
        return v


class UserCheckoutResponse(BaseModel):
    """
    Response model for user checkout session creation.

    Attributes:
        checkout_url (str): URL to redirect user to for payment.
        session_id (str): Stripe checkout session ID.
    """

    checkout_url: str
    session_id: str


# ============================================================================
# User Billing Profile Schemas
# ============================================================================


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
