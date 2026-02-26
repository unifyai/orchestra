"""Pydantic schemas for email authentication endpoints."""

from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class EmailRegisterRequest(BaseModel):
    """Request to register a new user with email/password."""

    email: EmailStr
    name: Optional[str] = None
    last_name: Optional[str] = None
    password: str = Field(..., min_length=8, max_length=128)
    captcha_token: Optional[str] = None  # Cloudflare Turnstile token

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class EmailVerifyRequest(BaseModel):
    """Request to verify a 6-digit email code."""

    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class EmailAuthenticateRequest(BaseModel):
    """Request to authenticate with email/password (login pre-validation)."""

    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    """Request to initiate a password reset."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Request to reset a password with a verification code."""

    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(..., min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    """Request to change password (authenticated user)."""

    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class ResendVerificationRequest(BaseModel):
    """Request to resend a verification code."""

    email: EmailStr
    purpose: str = Field(
        ...,
        pattern=r"^(signup|password_reset)$",
        description="Purpose of the verification: 'signup' or 'password_reset'",
    )


class AuthRegisterResponse(BaseModel):
    """Response after registration initiation."""

    email: str
    requires_verification: bool = True


class AuthVerifyResponse(BaseModel):
    """Response after successful email verification (user created)."""

    id: str
    email: str
    name: Optional[str] = None


class AuthenticateResponse(BaseModel):
    """Response after successful authentication."""

    id: str
    email: str
    name: Optional[str] = None
    image: Optional[str] = None
    mfa_required: bool = False


class ProvidersForEmailResponse(BaseModel):
    """Response listing linked providers for an email."""

    providers: List[str]


class EmailCredentialsResponse(BaseModel):
    """Response for email credential lookup (no sensitive data)."""

    has_email_account: bool
    email_verified: Optional[bool] = None
    created_at: Optional[str] = None
    password_changed_at: Optional[str] = None


class AuthErrorResponse(BaseModel):
    """Error response with optional provider hints."""

    error: str
    message: str
    providers: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# MFA (Phase 2) schemas
# ---------------------------------------------------------------------------


class MFASetupResponse(BaseModel):
    """Response after initiating TOTP setup."""

    qr_code_uri: str


class MFAConfirmRequest(BaseModel):
    """Request to confirm TOTP setup with a verification code."""

    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class MFAConfirmResponse(BaseModel):
    """Response after successful TOTP confirmation."""

    recovery_codes: List[str]


class MFAVerifyRequest(BaseModel):
    """Request to verify a TOTP code during login."""

    user_id: str
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class MFAVerifyResponse(BaseModel):
    """Response after successful TOTP verification during login."""

    success: bool = True


class MFAVerifyRecoveryRequest(BaseModel):
    """Request to verify a recovery code during login."""

    user_id: str
    code: str = Field(..., min_length=1, max_length=20)


class MFAVerifyRecoveryResponse(BaseModel):
    """Response after successful recovery code verification."""

    success: bool = True
    remaining_codes: int


class MFADisableRequest(BaseModel):
    """Request to disable MFA (requires current TOTP code)."""

    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class MFAStatusResponse(BaseModel):
    """Response for MFA status check."""

    enabled: bool
    method: Optional[str] = None
    confirmed_at: Optional[str] = None
    recovery_codes_remaining: int = 0


class MFARegenerateRecoveryResponse(BaseModel):
    """Response after regenerating recovery codes."""

    recovery_codes: List[str]
