from datetime import datetime
from typing import Literal, Optional
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from orchestra.web.api.utils.tax_id_validator import validate_tax_id_for_country

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
    providerAccountId: str
    access_token: str
    expires_at: int
    scope: str
    token_type: str
    id_token: str
    userId: Optional[str] = None


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


# -- Assistant hiring approval --
class AssistantHiringApprovalResponse(BaseModel):
    message: str
    assistant_hiring_approval: Optional[str]


class AssistantHiringOneTimeLinkClaimTokenRequest(BaseModel):
    token: str


class AssistantHiringApprovalUserStatus(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    assistant_hiring_approval: Optional[str]
    created_at: datetime


class AssistantHiringApprovalCreateLinkRequest(BaseModel):
    expires_in_days: int = 7


class AssistantHiringOneTimeLinkResponse(BaseModel):
    id: str
    token: str
    expires_at: datetime
    claimed_at: Optional[datetime] = None
    user_id: Optional[str] = None


# -- Business Classification for B2B/B2C Tax Compliance --


class BusinessAddress(BaseModel):
    """Business address information for tax purposes."""

    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state: Optional[str] = None
    country: str
    postal_code: Optional[str] = None


class BusinessInfo(BaseModel):
    """Business information for B2B accounts."""

    business_name: str
    tax_id: Optional[str] = None
    business_type: str  # 'corporation', 'llc', 'partnership', 'sole_proprietorship', etc.
    business_address: BusinessAddress
    tax_exempt: bool = False

    @model_validator(mode="after")
    def validate_tax_id_format(self):
        """Validate tax ID format based on country."""
        if not self.tax_id:
            return self

        country = self.business_address.country

        # Validate using python-stdnum
        validation_result = validate_tax_id_for_country(self.tax_id, country)

        if not validation_result["is_valid"]:
            raise ValueError(
                f"Invalid tax ID for {country}: {validation_result['error']}",
            )

        # Update with the formatted tax ID
        self.tax_id = validation_result["formatted_tax_id"] or self.tax_id
        return self


class UpdateAccountTypeRequest(BaseModel):
    """Request to update user account type."""

    account_type: Literal["individual", "business"]
    business_info: Optional[BusinessInfo] = None

    # User details (needed for user creation flow)
    email: Optional[str] = None
    name: Optional[str] = None
    last_name: Optional[str] = None

    @field_validator("business_info")
    @classmethod
    def validate_business_info(cls, v, info):
        """Ensure business_info is provided when account_type is 'business'."""
        if info.data.get("account_type") == "business" and not v:
            raise ValueError("business_info is required for business accounts")
        return v


class UpdateBusinessInfoRequest(BaseModel):
    """Request to update business information."""

    business_name: Optional[str] = None
    tax_id: Optional[str] = None
    business_type: Optional[str] = None
    business_address: Optional[BusinessAddress] = None
    tax_exempt: Optional[bool] = None


class OnboardingStatusResponse(BaseModel):
    """Response containing user's onboarding status."""

    onboarded: bool


class UpdateOnboardingStatusRequest(BaseModel):
    """Request to update user's onboarding status."""

    onboarded: bool


class BusinessVerificationRequest(BaseModel):
    """Request to verify a business account."""

    user_id: str


class UserBusinessStatusResponse(BaseModel):
    """Response containing user's business classification status."""

    account_type: str
    business_name: Optional[str] = None
    tax_id: Optional[str] = None
    business_type: Optional[str] = None
    business_verified: bool
    tax_exempt: bool
    tax_jurisdiction: Optional[str] = None
    business_address: Optional[BusinessAddress] = None


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
