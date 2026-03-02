"""
Email authentication & MFA endpoints.

Admin-key endpoints (called by the Next.js server on behalf of users):
  - POST /admin/auth/register
  - POST /admin/auth/verify-email
  - POST /admin/auth/authenticate
  - POST /admin/auth/forgot-password
  - POST /admin/auth/verify-code
  - POST /admin/auth/reset-password
  - POST /admin/auth/resend-verification
  - GET  /admin/auth/providers-for-email
  - GET  /admin/auth/onboarding-status-by-email → check onboarding status for OAuth sign-in
  - POST /admin/auth/mfa/verify          → validate TOTP code during login
  - POST /admin/auth/mfa/verify-recovery → validate recovery code during login
  - GET  /admin/auth/mfa/status-by-email → check MFA status for OAuth sign-in

User-API-key endpoints (called by the authenticated user):
  - POST /auth/set-password
  - POST /auth/change-password
  - POST /auth/mfa/setup         → generate TOTP secret, return QR URI
  - POST /auth/mfa/confirm       → validate first TOTP code, enable MFA
  - DELETE /auth/mfa             → disable MFA (requires current code)
  - POST /auth/mfa/recovery-codes → regenerate recovery codes
  - GET  /auth/mfa/status        → check MFA status
"""

import logging
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.email_account_dao import EmailAccountDAO
from orchestra.db.dao.email_verification_dao import (
    EmailVerificationDAO,
    check_user_agent,
    decode_verification_token,
    generate_verification_code,
    is_disposable_email,
    sign_verification_token,
    verify_turnstile_token,
)
from orchestra.db.dao.mfa_credential_dao import MFACredentialDAO, MFARecoveryDAO
from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.auth.schema import (
    AuthenticateResponse,
    AuthRegisterResponse,
    AuthVerifyResponse,
    ChangePasswordRequest,
    CreateUserRequest,
    EmailAuthenticateRequest,
    EmailCredentialsResponse,
    EmailRegisterRequest,
    EmailVerifyRequest,
    ForgotPasswordRequest,
    MFAConfirmRequest,
    MFAConfirmResponse,
    MFADisableRequest,
    MFARegenerateRecoveryResponse,
    MFASetupResponse,
    MFAStatusByEmailResponse,
    MFAStatusResponse,
    MFAVerifyRecoveryRequest,
    MFAVerifyRecoveryResponse,
    MFAVerifyRequest,
    MFAVerifyResponse,
    OnboardingStatusByEmailResponse,
    ProvidersForEmailResponse,
    ResendVerificationRequest,
    ResetPasswordWithTokenRequest,
    SetPasswordRequest,
    VerifyCodeResponse,
)

admin_router = APIRouter()
router = APIRouter()
logger = logging.getLogger(__name__)
ph = PasswordHasher()


# =============================================================================
# Admin-key endpoints (called by Next.js server)
# =============================================================================


@admin_router.post(
    "/auth/register",
    response_model=AuthRegisterResponse,
    status_code=status.HTTP_200_OK,
)
async def register(
    body: EmailRegisterRequest,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Initiate email registration.

    Validates input, checks for existing users and disposable emails,
    hashes the password, stores a pending verification entry, and sends
    a verification email. No User row is created at this stage.
    """
    email = body.email.lower().strip()

    # 0a. User-Agent heuristic check
    user_agent = request.headers.get("user-agent")
    if not check_user_agent(user_agent):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "suspicious_request",
                "message": "Request blocked. Please use a standard browser.",
            },
        )

    # 1. Check disposable email domain (cheap, no CAPTCHA needed)
    if is_disposable_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "disposable_email",
                "message": "Please use a permanent email address.",
            },
        )

    # 2. Check if email already registered (cheap DB lookup, no CAPTCHA needed)
    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if existing:
        user = existing[0][0]
        providers = UserDAO(session).get_linked_providers(user.id)

        # If the user already has an email/password account, give a simple message
        if "email" in providers:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "email_exists",
                    "message": "This email is already registered. Please sign in instead.",
                    "providers": providers,
                },
            )

        # User exists via OAuth only — tell them which provider to use
        provider_str = ", ".join(providers) if providers else "another method"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "email_exists",
                "message": (
                    f"This email is registered with {provider_str}. "
                    f"Please sign in with {provider_str}, then link "
                    f"email/password from your profile settings."
                ),
                "providers": providers,
            },
        )

    # 3. Validate CAPTCHA (Cloudflare Turnstile) — only for genuinely new registrations
    remote_ip = request.client.host if request.client else None
    captcha_ok = await verify_turnstile_token(body.captcha_token, remote_ip)
    if not captcha_ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "captcha_failed",
                "message": "CAPTCHA verification failed. Please try again.",
            },
        )

    # 3. Hash the password
    password_hash = ph.hash(body.password)

    # 4. Create verification entry (overwrites any existing pending signup)
    verification_dao = EmailVerificationDAO(session)
    code = generate_verification_code()
    verification_dao.create_signup_verification(
        email=email,
        code=code,
        password_hash=password_hash,
        name=body.name,
        last_name=body.last_name,
    )
    session.flush()

    # 5. Send verification email
    try:
        from orchestra.web.api.utils.email import send_email_async

        sent = await send_email_async(
            to_email=email,
            email_subject="Verify your Unify account",
            email_body=(
                f"<p>Thanks for signing up for Unify! Please use the code below "
                f"to verify the email address <strong>{email}</strong>.</p>"
                f"<p>Your verification code is:</p>"
                f"<p style='font-size: 24px; font-weight: bold; letter-spacing: 4px; "
                f"text-align: center; margin: 16px 0;'>{code}</p>"
                f"<p>This code expires in <strong>1 hour</strong>.</p>"
                f"<p style='color: #666; margin-top: 16px;'>If you did not create "
                f"a Unify account, you can safely ignore this email.</p>"
            ),
            from_email="hello@unify.ai",
            impersonate_email="hello@unify.ai",
        )
        if not sent:
            print(f"[LOCAL DEV] Verification code for {email}: {code}", flush=True)
    except Exception:
        logger.exception(f"Failed to send verification email to {email}")
        print(f"[LOCAL DEV] Verification code for {email}: {code}", flush=True)
        # Don't fail the registration — the user can resend

    session.commit()
    return AuthRegisterResponse(email=email)


@admin_router.post(
    "/auth/verify-code",
    response_model=VerifyCodeResponse,
    status_code=status.HTTP_200_OK,
)
def verify_code(
    body: EmailVerifyRequest,
    session: Session = Depends(get_db_session),
):
    """
    Unified code verification for both signup and password reset.

    Validates the 6-digit code, invalidates it (so it can't be re-used),
    but keeps the verification row (signup entries store name/password_hash
    that create-user needs). Returns a short-lived JWT token.

    Follow-up endpoints:
      - signup:         POST /auth/create-user    { token }
      - password_reset: POST /auth/reset-password { token, new_password }
    """
    email = body.email.lower().strip()
    purpose = body.purpose

    if purpose not in ("signup", "password_reset"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_purpose",
                "message": "Purpose must be 'signup' or 'password_reset'.",
            },
        )

    verification_dao = EmailVerificationDAO(session)
    verification = verification_dao.validate_code(email, body.code, purpose)

    if verification is None:
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_code",
                "message": "Invalid or expired code. Please try again or request a new one.",
            },
        )

    # Code is valid — invalidate it so it can't be re-used, but keep the
    # row intact (signup entries store name/password_hash needed by create-user).
    verification.code_hash = ""
    session.commit()

    token = sign_verification_token(email, purpose)
    return VerifyCodeResponse(token=token)


@admin_router.post(
    "/auth/create-user",
    response_model=AuthVerifyResponse,
    status_code=status.HTTP_200_OK,
)
def create_user_after_verification(
    body: CreateUserRequest,
    session: Session = Depends(get_db_session),
):
    """
    Create a User + EmailAccount after email verification.

    Accepts the JWT token from POST /auth/verify-code (purpose=signup).
    Reads stored name, last_name, and password_hash from the verification
    entry, creates the user, and deletes the entry.
    """
    email = decode_verification_token(body.token, expected_purpose="signup")

    # Check if user was created concurrently
    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if existing:
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "email_exists",
                "message": "This email is already registered.",
            },
        )

    # Retrieve signup data from the verification entry
    verification_dao = EmailVerificationDAO(session)
    verification = verification_dao.get_pending(email, "signup")
    if verification is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_pending_signup",
                "message": "No pending signup found. Please register again.",
            },
        )

    # Create User + EmailAccount in a single transaction
    user = user_dao.create(
        email=email,
        name=verification.name,
        last_name=verification.last_name,
    )
    session.flush()  # Get user.id

    api_key_dao = ApiKeyDAO(session)
    from orchestra.web.api.users.views import generate_key

    new_api_key = generate_key()
    api_key_dao.create(key=new_api_key, name="", user_id=user.id)

    # Seed default project for the new user.
    try:
        from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder

        nested = session.begin_nested()
        try:
            DefaultTasksSeeder.seed(session, user_id=str(user.id))
            nested.commit()
        except Exception:
            nested.rollback()
            logger.warning(
                f"Failed to seed default tasks for user {user.id} (rolled back savepoint)",
                exc_info=True,
            )
    except Exception as e:
        logger.warning(f"Failed to seed default tasks for user {user.id}: {e}")

    email_account_dao = EmailAccountDAO(session)
    email_account_dao.create(
        user_id=user.id,
        password_hash=verification.password_hash,
        email_verified=True,
    )

    # Initialize onboarding status for the new user
    onboarding_dao = OnboardingStatusDAO(session)
    onboarding_dao.create(user_id=user.id, current_step="workspace_setup")

    # Delete the verification entry
    verification_dao.delete(verification.id)
    session.commit()

    return AuthVerifyResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
    )


@admin_router.post(
    "/auth/authenticate",
    response_model=AuthenticateResponse,
    status_code=status.HTTP_200_OK,
)
def authenticate(
    body: EmailAuthenticateRequest,
    session: Session = Depends(get_db_session),
):
    """
    Pre-validate email + password credentials.

    Called by the frontend before NextAuth signIn("credentials") to get
    specific error messages. Returns user info on success.
    """
    email = body.email.lower().strip()

    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_credentials",
                "message": "Invalid email or password.",
            },
        )

    user = existing[0][0]
    email_account_dao = EmailAccountDAO(session)
    email_account = email_account_dao.get_by_user_id(user.id)

    if email_account is None:
        # User exists but has no email/password — they signed up via OAuth
        providers = UserDAO(session).get_linked_providers(user.id)
        provider_str = ", ".join(providers) if providers else "another method"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "no_email_account",
                "message": (
                    f"This email is registered with {provider_str}. "
                    f"Please sign in with {provider_str}."
                ),
                "providers": providers,
            },
        )

    # Verify password
    try:
        ph.verify(email_account.password_hash, body.password)
    except VerifyMismatchError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_credentials",
                "message": "Invalid email or password.",
            },
        )

    # Check if password hash needs rehashing (argon2-cffi feature)
    if ph.check_needs_rehash(email_account.password_hash):
        email_account.password_hash = ph.hash(body.password)

    # Check email_verified
    if not email_account.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "email_not_verified",
                "message": "Your email address has not been verified.",
            },
        )

    # Check for enabled MFA credentials
    mfa_dao = MFACredentialDAO(session)
    mfa_required = mfa_dao.has_enabled_mfa(user.id)

    # Derive onboarding step from OnboardingStatus table
    onboarding_dao = OnboardingStatusDAO(session)
    onboarding_status = onboarding_dao.get_by_user_id(user.id)
    onboarding_step = (
        onboarding_status.current_step if onboarding_status else "completed"
    )

    session.commit()
    return AuthenticateResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        last_name=user.last_name,
        image=user.image,
        mfa_required=mfa_required,
        onboarding_step=onboarding_step,
    )


@admin_router.post(
    "/auth/forgot-password",
    status_code=status.HTTP_200_OK,
)
async def forgot_password(
    body: ForgotPasswordRequest,
    session: Session = Depends(get_db_session),
):
    """
    Initiate a password reset flow.

    Always returns 200 to prevent email enumeration.
    """
    email = body.email.lower().strip()

    # Look up user + email account silently
    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if not existing:
        return {"message": "If an account exists, a reset code has been sent."}

    user = existing[0][0]
    email_account_dao = EmailAccountDAO(session)
    email_account = email_account_dao.get_by_user_id(user.id)
    if email_account is None:
        return {"message": "If an account exists, a reset code has been sent."}

    # Create reset verification
    verification_dao = EmailVerificationDAO(session)
    code = generate_verification_code()
    verification_dao.create_password_reset_verification(email=email, code=code)
    session.flush()

    # Send reset email
    try:
        from orchestra.web.api.utils.email import send_email_async

        sent = await send_email_async(
            to_email=email,
            email_subject="Reset your Unify password",
            email_body=(
                f"<p>We received a request to reset the password for the Unify account "
                f"associated with <strong>{email}</strong>.</p>"
                f"<p>Your password reset code is:</p>"
                f"<p style='font-size: 24px; font-weight: bold; letter-spacing: 4px; "
                f"text-align: center; margin: 16px 0;'>{code}</p>"
                f"<p>This code expires in <strong>10 minutes</strong>.</p>"
                f"<p style='color: #666; margin-top: 16px;'>If you did not request "
                f"a password reset, you can safely ignore this email — your password "
                f"will remain unchanged. No one can access your account without this code.</p>"
            ),
            from_email="hello@unify.ai",
            impersonate_email="hello@unify.ai",
        )
        if not sent:
            print(f"[LOCAL DEV] Password reset code for {email}: {code}", flush=True)
    except Exception:
        logger.exception(f"Failed to send password reset email to {email}")
        print(f"[LOCAL DEV] Password reset code for {email}: {code}", flush=True)

    session.commit()
    return {"message": "If an account exists, a reset code has been sent."}


@admin_router.post(
    "/auth/reset-password",
    status_code=status.HTTP_200_OK,
)
def reset_password(
    body: ResetPasswordWithTokenRequest,
    session: Session = Depends(get_db_session),
):
    """
    Reset a password using a verification token from POST /auth/verify-code.

    Validates the token, updates the password hash, sets password_changed_at
    for session invalidation, and cleans up any leftover verification entries.
    """
    email = decode_verification_token(body.token, expected_purpose="password_reset")

    # Find user + email account
    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if not existing:
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "user_not_found",
                "message": "No account found for this email.",
            },
        )

    user = existing[0][0]
    email_account_dao = EmailAccountDAO(session)
    result = email_account_dao.update_password(
        user_id=user.id,
        new_password_hash=ph.hash(body.new_password),
    )
    if result is None:
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_email_account",
                "message": "No email/password account found for this email.",
            },
        )

    # Clean up any leftover verification entries for this email
    verification_dao = EmailVerificationDAO(session)
    verification_dao.delete_by_email_and_purpose(email, "password_reset")
    session.commit()

    return {"message": "Password has been reset successfully."}


@admin_router.post(
    "/auth/resend-verification",
    status_code=status.HTTP_200_OK,
)
async def resend_verification(
    body: ResendVerificationRequest,
    session: Session = Depends(get_db_session),
):
    """
    Resend a verification code for signup or password reset.

    Creates a new code (overwriting any previous one for the same
    email + purpose) and sends it.  Enforces a 60-second cooldown
    between resends for the same email+purpose to prevent abuse.
    """
    email = body.email.lower().strip()

    verification_dao = EmailVerificationDAO(session)

    # Cooldown: reject if the most recent entry for this email+purpose
    # was created less than 60 seconds ago.
    existing_for_cooldown = verification_dao.get_pending(email, body.purpose)
    if existing_for_cooldown and existing_for_cooldown.created_at:
        created = existing_for_cooldown.created_at
        # Ensure timezone-aware comparison (TIMESTAMP columns may be naive).
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
        if age_seconds < 60:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "cooldown",
                    "message": "Please wait before requesting another code.",
                    "retry_after": int(60 - age_seconds),
                },
            )

    if body.purpose == "signup":
        # Get existing pending signup to preserve the password_hash and name
        existing = verification_dao.get_pending(email, "signup")
        if existing is None:
            # No pending signup — silently return to prevent enumeration
            return {
                "message": "If a pending verification exists, a new code has been sent.",
            }

        code = generate_verification_code()
        verification_dao.create_signup_verification(
            email=email,
            code=code,
            password_hash=existing.password_hash,
            name=existing.name,
            last_name=existing.last_name,
        )
    elif body.purpose == "password_reset":
        # Check that user + email account exist
        user_dao = UserDAO(session)
        user_rows = user_dao.filter(email=email)
        if not user_rows:
            return {
                "message": "If a pending verification exists, a new code has been sent.",
            }

        user = user_rows[0][0]
        email_account_dao = EmailAccountDAO(session)
        if email_account_dao.get_by_user_id(user.id) is None:
            return {
                "message": "If a pending verification exists, a new code has been sent.",
            }

        code = generate_verification_code()
        verification_dao.create_password_reset_verification(email=email, code=code)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_purpose", "message": "Invalid purpose."},
        )

    session.flush()

    # Send email
    try:
        from orchestra.web.api.utils.email import send_email_async

        if body.purpose == "signup":
            subject = "Verify your Unify account"
            body_html = (
                f"<p>Thanks for signing up for Unify! Please use the code below "
                f"to verify the email address <strong>{email}</strong>.</p>"
                f"<p>Your verification code is:</p>"
                f"<p style='font-size: 24px; font-weight: bold; letter-spacing: 4px; "
                f"text-align: center; margin: 16px 0;'>{code}</p>"
                f"<p>This code expires in <strong>1 hour</strong>.</p>"
                f"<p style='color: #666; margin-top: 16px;'>If you did not create "
                f"a Unify account, you can safely ignore this email.</p>"
            )
        else:
            subject = "Reset your Unify password"
            body_html = (
                f"<p>We received a request to reset the password for the Unify account "
                f"associated with <strong>{email}</strong>.</p>"
                f"<p>Your password reset code is:</p>"
                f"<p style='font-size: 24px; font-weight: bold; letter-spacing: 4px; "
                f"text-align: center; margin: 16px 0;'>{code}</p>"
                f"<p>This code expires in <strong>10 minutes</strong>.</p>"
                f"<p style='color: #666; margin-top: 16px;'>If you did not request "
                f"a password reset, you can safely ignore this email — your password "
                f"will remain unchanged. No one can access your account without this code.</p>"
            )
        sent = await send_email_async(
            to_email=email,
            email_subject=subject,
            email_body=body_html,
            from_email="hello@unify.ai",
            impersonate_email="hello@unify.ai",
        )
        if not sent:
            print(f"[LOCAL DEV] Verification code for {email}: {code}", flush=True)
    except Exception:
        logger.exception(f"Failed to resend verification email to {email}")
        print(f"[LOCAL DEV] Verification code for {email}: {code}", flush=True)

    session.commit()
    return {"message": "If a pending verification exists, a new code has been sent."}


@admin_router.get(
    "/auth/providers-for-email",
    response_model=ProvidersForEmailResponse,
    status_code=status.HTTP_200_OK,
)
def providers_for_email(
    email: str,
    session: Session = Depends(get_db_session),
):
    """
    Return linked providers for an email address.

    Used by the frontend to show provider-aware error messages after
    a failed login attempt. Only returns provider names — no sensitive data.
    """
    email = email.lower().strip()

    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if not existing:
        return ProvidersForEmailResponse(providers=[])

    user = existing[0][0]
    providers = UserDAO(session).get_linked_providers(user.id)
    return ProvidersForEmailResponse(providers=providers)


@admin_router.get(
    "/auth/email-credentials",
    response_model=EmailCredentialsResponse,
    status_code=status.HTTP_200_OK,
)
def get_email_credentials(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    """
    Return email credential metadata for a user (no sensitive data).

    Returns whether the user has an email/password account, along with
    non-sensitive metadata (verified status, timestamps). Never exposes
    the password hash.
    """
    email_account_dao = EmailAccountDAO(session)
    email_account = email_account_dao.get_by_user_id(user_id)

    if email_account is None:
        return EmailCredentialsResponse(has_email_account=False)

    return EmailCredentialsResponse(
        has_email_account=True,
        email_verified=email_account.email_verified,
        created_at=str(email_account.created_at) if email_account.created_at else None,
        password_changed_at=(
            str(email_account.password_changed_at)
            if email_account.password_changed_at
            else None
        ),
    )


# =============================================================================
# User-API-key endpoint (called by authenticated user)
# =============================================================================


@router.post(
    "/auth/change-password",
    status_code=status.HTTP_200_OK,
)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Change password for an authenticated user.

    Requires the current password for verification. Sets password_changed_at
    to invalidate other sessions.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Authentication required."},
        )

    email_account_dao = EmailAccountDAO(session)
    email_account = email_account_dao.get_by_user_id(user_id)

    if email_account is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_email_account",
                "message": "No email/password account found. You may be using OAuth only.",
            },
        )

    # Verify current password
    try:
        ph.verify(email_account.password_hash, body.current_password)
    except VerifyMismatchError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_credentials",
                "message": "Current password is incorrect.",
            },
        )

    # Update password
    email_account_dao.update_password(
        user_id=user_id,
        new_password_hash=ph.hash(body.new_password),
    )
    session.commit()

    return {"message": "Password changed successfully."}


@router.post(
    "/auth/set-password",
    status_code=status.HTTP_200_OK,
)
def set_password(
    body: SetPasswordRequest,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Set a password for an OAuth-only user.

    Allows users who signed up via OAuth (Google/GitHub) to add email/password
    credentials so they can also sign in with their email and a password.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Authentication required."},
        )

    email_account_dao = EmailAccountDAO(session)
    existing = email_account_dao.get_by_user_id(user_id)

    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "password_already_set",
                "message": "You already have a password. Try changing your password if you want to update it.",
            },
        )

    email_account_dao.create(
        user_id=user_id,
        password_hash=ph.hash(body.new_password),
    )
    session.commit()

    return {
        "message": "Password set successfully. You can now sign in with your email and password.",
    }


# =============================================================================
# MFA — User-API-key endpoints (authenticated user)
# =============================================================================


@router.post(
    "/auth/mfa/setup",
    response_model=MFASetupResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_setup(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Initiate TOTP setup.

    Generates a new TOTP secret, encrypts it, stores a pending
    MFACredential, and returns the provisioning URI for QR-code display.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Authentication required."},
        )

    mfa_dao = MFACredentialDAO(session)

    # Block if user already has an enabled TOTP credential
    existing = mfa_dao.get_enabled_totp(user_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "mfa_already_enabled",
                "message": "Two-factor authentication is already enabled.",
            },
        )

    # Get user email for the provisioning URI
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(user_id)
    if not user_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "user_not_found", "message": "User not found."},
        )
    user = user_row[0]

    credential, provisioning_uri = mfa_dao.create_totp_credential(
        user_id=user_id,
        user_email=user.email,
    )
    session.commit()

    return MFASetupResponse(qr_code_uri=provisioning_uri)


@router.post(
    "/auth/mfa/confirm",
    response_model=MFAConfirmResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_confirm(
    body: MFAConfirmRequest,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Confirm TOTP setup.

    Validates the user's first TOTP code, enables the credential,
    generates recovery codes, and returns them.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Authentication required."},
        )

    mfa_dao = MFACredentialDAO(session)
    credential = mfa_dao.get_pending_totp(user_id)

    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_pending_setup",
                "message": "No pending TOTP setup found. Please initiate setup first.",
            },
        )

    # Verify the TOTP code against the pending credential
    if not mfa_dao.verify_totp_code(credential, body.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_code",
                "message": "Invalid TOTP code. Please try again.",
            },
        )

    # Enable the credential
    mfa_dao.confirm_totp(credential)

    # Generate recovery codes
    recovery_dao = MFARecoveryDAO(session)
    plaintext_codes = recovery_dao.generate_and_store(user_id)

    session.commit()
    return MFAConfirmResponse(recovery_codes=plaintext_codes)


@router.delete(
    "/auth/mfa",
    status_code=status.HTTP_200_OK,
)
def mfa_disable(
    body: MFADisableRequest,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Disable MFA.

    Requires a valid TOTP code for confirmation. Deletes the credential
    and all recovery codes.

    Blocks the request if the user is a member of any organization that
    requires MFA (``Organization.require_mfa = True``).
    """
    from orchestra.db.dao.organization_dao import OrganizationDAO

    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Authentication required."},
        )

    mfa_dao = MFACredentialDAO(session)
    credential = mfa_dao.get_enabled_totp(user_id)

    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "mfa_not_enabled",
                "message": "Two-factor authentication is not enabled.",
            },
        )

    # Check if any org requires MFA for this user
    org_dao = OrganizationDAO(session)
    blocking_orgs = org_dao.get_mfa_requiring_orgs_for_user(user_id)
    if blocking_orgs:
        org_names = [org.name for org in blocking_orgs]
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "mfa_required_by_org",
                "message": (
                    f"MFA is required by {org_names[0]}. "
                    f"You cannot disable it while you are a member."
                ),
                "org_names": org_names,
            },
        )

    # Verify the code before disabling (TOTP or recovery code)
    recovery_dao = MFARecoveryDAO(session)

    if body.code:
        # Verify TOTP code
        if not mfa_dao.verify_totp_code(credential, body.code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_code",
                    "message": "Invalid TOTP code. Please try again.",
                },
            )
    elif body.recovery_code:
        # Verify recovery code
        remaining = recovery_dao.verify_and_consume(user_id, body.recovery_code)
        if remaining is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_recovery_code",
                    "message": "Invalid recovery code. Please try again.",
                },
            )

    # Delete credential and recovery codes
    mfa_dao.delete_credential(credential)
    recovery_dao.delete_all_for_user(user_id)

    session.commit()
    return {"message": "Two-factor authentication has been disabled."}


@router.post(
    "/auth/mfa/recovery-codes",
    response_model=MFARegenerateRecoveryResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_regenerate_recovery_codes(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Regenerate recovery codes.

    Deletes existing codes and generates a fresh set. The user must
    have MFA enabled.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Authentication required."},
        )

    mfa_dao = MFACredentialDAO(session)
    if not mfa_dao.has_enabled_mfa(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "mfa_not_enabled",
                "message": "Two-factor authentication is not enabled.",
            },
        )

    recovery_dao = MFARecoveryDAO(session)
    plaintext_codes = recovery_dao.generate_and_store(user_id)

    session.commit()
    return MFARegenerateRecoveryResponse(recovery_codes=plaintext_codes)


@router.get(
    "/auth/mfa/status",
    response_model=MFAStatusResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Check the user's MFA status.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "Authentication required."},
        )

    mfa_dao = MFACredentialDAO(session)
    credential = mfa_dao.get_enabled_totp(user_id)

    if credential is None:
        return MFAStatusResponse(enabled=False)

    recovery_dao = MFARecoveryDAO(session)
    remaining = recovery_dao.remaining_count(user_id)

    return MFAStatusResponse(
        enabled=True,
        method="totp",
        confirmed_at=(
            str(credential.confirmed_at) if credential.confirmed_at else None
        ),
        recovery_codes_remaining=remaining,
    )


# =============================================================================
# MFA — Admin-key endpoints (called by Next.js server during login)
# =============================================================================


@admin_router.post(
    "/auth/mfa/verify",
    response_model=MFAVerifyResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_verify(
    body: MFAVerifyRequest,
    session: Session = Depends(get_db_session),
):
    """
    Verify a TOTP code during the login flow.

    Called by the Next.js server after the user enters their 2FA code
    on the /login/mfa page.
    """
    mfa_dao = MFACredentialDAO(session)
    credential = mfa_dao.get_enabled_totp(body.user_id)

    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "mfa_not_enabled",
                "message": "Two-factor authentication is not enabled.",
            },
        )

    if not mfa_dao.verify_totp_code(credential, body.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_code",
                "message": "Invalid or expired TOTP code.",
            },
        )

    session.commit()
    return MFAVerifyResponse(success=True)


@admin_router.post(
    "/auth/mfa/verify-recovery",
    response_model=MFAVerifyRecoveryResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_verify_recovery(
    body: MFAVerifyRecoveryRequest,
    session: Session = Depends(get_db_session),
):
    """
    Verify a recovery code during the login flow.

    Called by the Next.js server when the user uses a recovery code
    instead of a TOTP code.
    """
    mfa_dao = MFACredentialDAO(session)
    if not mfa_dao.has_enabled_mfa(body.user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "mfa_not_enabled",
                "message": "Two-factor authentication is not enabled.",
            },
        )

    recovery_dao = MFARecoveryDAO(session)
    remaining = recovery_dao.verify_and_consume(body.user_id, body.code)

    if remaining is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_recovery_code",
                "message": "Invalid or already used recovery code.",
            },
        )

    session.commit()
    return MFAVerifyRecoveryResponse(success=True, remaining_codes=remaining)


@admin_router.get(
    "/auth/mfa/status-by-email",
    response_model=MFAStatusByEmailResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_status_by_email(
    email: str,
    session: Session = Depends(get_db_session),
):
    """
    Check whether a user has MFA enabled, given their email address.

    Called by the Next.js server during OAuth sign-in (jwt callback)
    to determine whether the OAuth user should be prompted for TOTP
    verification before completing login.
    """
    email = email.lower().strip()

    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if not existing:
        return MFAStatusByEmailResponse(user_found=False, mfa_enabled=False)

    user = existing[0][0]
    mfa_dao = MFACredentialDAO(session)
    has_mfa = mfa_dao.has_enabled_mfa(user.id)

    return MFAStatusByEmailResponse(user_found=True, mfa_enabled=has_mfa)


@admin_router.get(
    "/auth/onboarding-status-by-email",
    response_model=OnboardingStatusByEmailResponse,
    status_code=status.HTTP_200_OK,
)
def onboarding_status_by_email(
    email: str,
    session: Session = Depends(get_db_session),
):
    """
    Check a user's onboarding status, given their email address.

    Called by the Next.js server during OAuth sign-in (jwt callback)
    to determine whether the user needs onboarding, instead of relying
    on the ``trigger === 'signUp'`` heuristic which fires even when an
    existing email user links a new OAuth provider.
    """
    email = email.lower().strip()

    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if not existing:
        return OnboardingStatusByEmailResponse(
            user_found=False,
            onboarding_step="workspace_setup",
        )

    user = existing[0][0]
    onboarding_dao = OnboardingStatusDAO(session)
    onboarding_status = onboarding_dao.get_by_user_id(user.id)
    step = onboarding_status.current_step if onboarding_status else "completed"

    return OnboardingStatusByEmailResponse(user_found=True, onboarding_step=step)
