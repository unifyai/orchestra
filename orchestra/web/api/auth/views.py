"""
Email authentication endpoints (Phase 1).

Admin-key endpoints (called by the Next.js server on behalf of users):
  - POST /admin/auth/register
  - POST /admin/auth/verify-email
  - POST /admin/auth/authenticate
  - POST /admin/auth/forgot-password
  - POST /admin/auth/reset-password
  - POST /admin/auth/resend-verification
  - GET  /admin/auth/providers-for-email

User-API-key endpoint (called by the authenticated user):
  - POST /auth/change-password
"""

import logging
from typing import List

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.account_dao import AccountDAO
from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.email_account_dao import EmailAccountDAO
from orchestra.db.dao.email_verification_dao import (
    EmailVerificationDAO,
    generate_verification_code,
    is_disposable_email,
    verify_turnstile_token,
)
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.auth.schema import (
    AuthenticateResponse,
    AuthRegisterResponse,
    AuthVerifyResponse,
    ChangePasswordRequest,
    EmailAuthenticateRequest,
    EmailCredentialsResponse,
    EmailRegisterRequest,
    EmailVerifyRequest,
    ForgotPasswordRequest,
    ProvidersForEmailResponse,
    ResendVerificationRequest,
    ResetPasswordRequest,
)

admin_router = APIRouter()
router = APIRouter()
logger = logging.getLogger(__name__)
ph = PasswordHasher()


def _get_linked_providers(
    user_id: str,
    session: Session,
) -> List[str]:
    """Get the list of auth providers linked to a user via UserDAO."""
    user_dao = UserDAO(session)
    account_dao = AccountDAO(session)
    email_account_dao = EmailAccountDAO(session)
    return user_dao.get_linked_providers(user_id, account_dao, email_account_dao)


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

    # 0. Validate CAPTCHA (Cloudflare Turnstile)
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

    # 1. Check disposable email domain
    if is_disposable_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "disposable_email",
                "message": "Please use a permanent email address.",
            },
        )

    # 2. Check if email already registered
    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if existing:
        user = existing[0][0]
        providers = _get_linked_providers(user.id, session)
        provider_str = ", ".join(providers) if providers else "another method"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "email_exists",
                "message": (
                    f"This email is already registered with {provider_str}. "
                    f"Please sign in with {provider_str}, then link "
                    f"email/password from your profile settings."
                ),
                "providers": providers,
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
                f"<p>Your verification code is: <strong>{code}</strong></p>"
                f"<p>This code expires in 1 hour.</p>"
            ),
            from_email="hello@unify.ai",
            impersonate_email="hello@unify.ai",
        )
        if not sent:
            logger.warning(f"[LOCAL DEV] Verification code for {email}: {code}")
    except Exception:
        logger.exception(f"Failed to send verification email to {email}")
        logger.warning(f"[LOCAL DEV] Verification code for {email}: {code}")
        # Don't fail the registration — the user can resend

    session.commit()
    return AuthRegisterResponse(email=email)


@admin_router.post(
    "/auth/verify-email",
    response_model=AuthVerifyResponse,
    status_code=status.HTTP_200_OK,
)
def verify_email(
    body: EmailVerifyRequest,
    session: Session = Depends(get_db_session),
):
    """
    Verify a 6-digit email code and create the User + EmailAccount.

    This is a single atomic transaction: validate the code, create the
    User row, create the EmailAccount row, delete the verification entry.
    """
    email = body.email.lower().strip()

    verification_dao = EmailVerificationDAO(session)
    verification = verification_dao.validate_code(email, body.code, "signup")

    if verification is None:
        # Commit the incremented attempt counter
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_code",
                "message": (
                    "Invalid or expired verification code. "
                    "Please check the code and try again."
                ),
            },
        )

    # Check if user was created concurrently
    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if existing:
        # User already exists — clean up the verification entry
        verification_dao.delete(verification.id)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "email_exists",
                "message": "This email is already registered.",
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
    # Use a savepoint so that a seeder failure doesn't poison the
    # outer transaction (PostgreSQL aborts all subsequent statements
    # after an error unless it's isolated in a savepoint).
    try:
        from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder

        nested = session.begin_nested()
        try:
            DefaultTasksSeeder.seed(session, user_id=user.id)
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
        providers = _get_linked_providers(user.id, session)
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

    session.commit()
    return AuthenticateResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        image=user.image,
        mfa_required=False,
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
            logger.warning(f"[LOCAL DEV] Password reset code for {email}: {code}")
    except Exception:
        logger.exception(f"Failed to send password reset email to {email}")
        logger.warning(f"[LOCAL DEV] Password reset code for {email}: {code}")

    session.commit()
    return {"message": "If an account exists, a reset code has been sent."}


@admin_router.post(
    "/auth/reset-password",
    status_code=status.HTTP_200_OK,
)
def reset_password(
    body: ResetPasswordRequest,
    session: Session = Depends(get_db_session),
):
    """
    Reset a password using a verification code.

    Validates the code, updates the password hash, sets password_changed_at
    for session invalidation, and deletes the verification entry.
    """
    email = body.email.lower().strip()

    verification_dao = EmailVerificationDAO(session)
    verification = verification_dao.validate_code(email, body.code, "password_reset")

    if verification is None:
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_code",
                "message": (
                    "Invalid or expired reset code. " "Please request a new one."
                ),
            },
        )

    # Find user + email account
    user_dao = UserDAO(session)
    existing = user_dao.filter(email=email)
    if not existing:
        verification_dao.delete(verification.id)
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
        verification_dao.delete(verification.id)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_email_account",
                "message": "No email/password account found for this email.",
            },
        )

    # Delete the verification entry
    verification_dao.delete(verification.id)
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
    email + purpose) and sends it.
    """
    email = body.email.lower().strip()

    verification_dao = EmailVerificationDAO(session)

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
                f"<p>Your verification code is: <strong>{code}</strong></p>"
                f"<p>This code expires in 1 hour.</p>"
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
            logger.warning(f"[LOCAL DEV] Verification code for {email}: {code}")
    except Exception:
        logger.exception(f"Failed to resend verification email to {email}")
        logger.warning(f"[LOCAL DEV] Verification code for {email}: {code}")

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
    providers = _get_linked_providers(user.id, session)
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
