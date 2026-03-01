"""Pydantic schemas for email authentication endpoints."""

import re
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Shared password validation
# ---------------------------------------------------------------------------

_PASSWORD_MIN_LENGTH = 8
_PASSWORD_MAX_LENGTH = 128

# Each rule: (compiled regex, human-readable message)
_PASSWORD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[a-z]"), "at least one lowercase letter"),
    (re.compile(r"[A-Z]"), "at least one uppercase letter"),
    (re.compile(r"\d"), "at least one digit"),
    (re.compile(r"[^A-Za-z0-9]"), "at least one special character"),
]


def _validate_password_strength(password: str) -> str:
    """
    Validate password strength rules.

    Requires:
      - 8–128 characters
      - At least one lowercase letter
      - At least one uppercase letter
      - At least one digit
      - At least one special character (non-alphanumeric)

    Raises ``ValueError`` with a descriptive message on failure.
    """
    if len(password) < _PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must be at least {_PASSWORD_MIN_LENGTH} characters.",
        )

    missing = [msg for pattern, msg in _PASSWORD_RULES if not pattern.search(password)]
    if missing:
        raise ValueError(
            "Password must contain " + ", ".join(missing) + ".",
        )

    return password


class EmailRegisterRequest(BaseModel):
    """Request to register a new user with email/password."""

    email: EmailStr
    name: Optional[str] = None
    last_name: Optional[str] = None
    password: str = Field(
        ...,
        min_length=_PASSWORD_MIN_LENGTH,
        max_length=_PASSWORD_MAX_LENGTH,
    )
    captcha_token: Optional[str] = None  # Cloudflare Turnstile token

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class EmailVerifyRequest(BaseModel):
    """Request to verify a 6-digit email code (with purpose)."""

    email: EmailStr
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
    purpose: str = Field(
        default="signup",
        description="Purpose of the verification: 'signup' or 'password_reset'",
    )


class VerifyCodeResponse(BaseModel):
    """Response after successful code verification — contains a short-lived token."""

    token: str
    message: str = "Code verified."


class CreateUserRequest(BaseModel):
    """Request to create a user after email verification (token-based)."""

    token: str


class ResetPasswordWithTokenRequest(BaseModel):
    """Request to reset password using a verification token (no raw code)."""

    token: str
    new_password: str = Field(
        ...,
        min_length=_PASSWORD_MIN_LENGTH,
        max_length=_PASSWORD_MAX_LENGTH,
    )

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


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
    new_password: str = Field(
        ...,
        min_length=_PASSWORD_MIN_LENGTH,
        max_length=_PASSWORD_MAX_LENGTH,
    )

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class ChangePasswordRequest(BaseModel):
    """Request to change password (authenticated user)."""

    current_password: str
    new_password: str = Field(
        ...,
        min_length=_PASSWORD_MIN_LENGTH,
        max_length=_PASSWORD_MAX_LENGTH,
    )

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class SetPasswordRequest(BaseModel):
    """Request to set a password for an OAuth-only user."""

    new_password: str = Field(
        ...,
        min_length=_PASSWORD_MIN_LENGTH,
        max_length=_PASSWORD_MAX_LENGTH,
    )

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


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
    last_name: Optional[str] = None
    image: Optional[str] = None
    mfa_required: bool = False
    onboarding_step: str = "completed"


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
    """Request to disable MFA (requires current TOTP code or recovery code)."""

    code: Optional[str] = Field(None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    recovery_code: Optional[str] = Field(None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def require_one_code(self) -> "MFADisableRequest":
        if not self.code and not self.recovery_code:
            raise ValueError(
                "Either 'code' (TOTP) or 'recovery_code' must be provided.",
            )
        return self


class MFAStatusResponse(BaseModel):
    """Response for MFA status check."""

    enabled: bool
    method: Optional[str] = None
    confirmed_at: Optional[str] = None
    recovery_codes_remaining: int = 0


class MFARegenerateRecoveryResponse(BaseModel):
    """Response after regenerating recovery codes."""

    recovery_codes: List[str]


class MFAStatusByEmailResponse(BaseModel):
    """Response for checking MFA status by email (admin endpoint)."""

    user_found: bool
    mfa_enabled: bool = False
