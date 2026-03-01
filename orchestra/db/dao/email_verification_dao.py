"""
Data Access Object for EmailVerification (signup and password reset codes).

Also contains:
- Anti-abuse utilities (disposable email blocking, Turnstile CAPTCHA, UA heuristics).
- JWT verification-token helpers for the two-step verify-code → action flow.
"""

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import jwt
from disposable_email_domains import blocklist as disposable_blocklist
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import EmailVerification
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# TTL constants
SIGNUP_TTL_HOURS = 1
PASSWORD_RESET_TTL_MINUTES = 10
MAX_ATTEMPTS = 5

# Verification constants
VERIFY_TOKEN_TTL_MINUTES = 5
VERIFY_TOKEN_SECRET = os.environ.get("ORCHESTRA_ADMIN_KEY")

# Turnstile constants
TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT_SECONDS = 5


# ---------------------------------------------------------------------------
# Verification token helpers (JWT)
# ---------------------------------------------------------------------------


def sign_verification_token(email: str, purpose: str) -> str:
    """
    Create a short-lived JWT proving the email code was verified.

    :param email: The verified email address.
    :param purpose: ``"signup"`` or ``"password_reset"``.
    :return: Encoded JWT string.
    """
    payload = {
        "sub": email,
        "purpose": purpose,
        "exp": datetime.now(tz=timezone.utc)
        + timedelta(minutes=VERIFY_TOKEN_TTL_MINUTES),
        "iat": datetime.now(tz=timezone.utc),
    }
    return jwt.encode(payload, VERIFY_TOKEN_SECRET, algorithm="HS256")


def decode_verification_token(token: str, expected_purpose: str) -> str:
    """
    Decode and validate a verification token.

    :param token: The JWT string.
    :param expected_purpose: Required purpose (``"signup"`` or ``"password_reset"``).
    :return: The email address from the token.
    :raises HTTPException: On expired, invalid, or wrong-purpose tokens.
    """
    try:
        payload = jwt.decode(token, VERIFY_TOKEN_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "token_expired",
                "message": "Verification token has expired. Please verify the code again.",
            },
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_token",
                "message": "Invalid verification token.",
            },
        )

    if payload.get("purpose") != expected_purpose:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "wrong_purpose",
                "message": "This token cannot be used for this action.",
            },
        )

    return payload["sub"]


# ---------------------------------------------------------------------------
# User-Agent heuristics
# ---------------------------------------------------------------------------

# Known bot / automation user-agent substrings (case-insensitive).
_BOT_UA_PATTERNS: list[re.Pattern] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bcurl\b",
        r"\bwget\b",
        r"\bpython-requests\b",
        r"\bhttpie\b",
        r"\bpostman\b",
        r"\bscrapy\b",
        r"\bphantomjs\b",
        r"\bheadless\b",
        r"\bbot\b",
        r"\bcrawler\b",
        r"\bspider\b",
        r"\bsemrush\b",
        r"\bahrefs\b",
    )
]


def generate_verification_code() -> str:
    """Generate a cryptographically random 6-digit code."""
    return str(secrets.randbelow(900000) + 100000)


def hash_code(code: str) -> str:
    """Hash a verification code using SHA-256."""
    return hashlib.sha256(code.encode()).hexdigest()


def is_disposable_email(email: str) -> bool:
    """Check if an email domain is in the disposable email blocklist."""
    domain = email.rsplit("@", 1)[-1].lower()
    return domain in disposable_blocklist


def check_user_agent(user_agent: Optional[str]) -> bool:
    """
    Return ``True`` if the User-Agent looks like a legitimate browser.

    Returns ``False`` (suspicious) when:
      - The header is missing or empty.
      - The UA matches a known bot / automation tool pattern.

    This is a *heuristic* — it's trivially spoofable and should be used
    alongside other anti-abuse measures (Turnstile, rate limiting, etc.),
    not as a sole gatekeeper.
    """
    if not user_agent or not user_agent.strip():
        logger.warning("Suspicious request: missing or empty User-Agent header")
        return False

    for pattern in _BOT_UA_PATTERNS:
        if pattern.search(user_agent):
            logger.warning(
                f"Suspicious User-Agent blocked: {user_agent!r} "
                f"(matched {pattern.pattern!r})",
            )
            return False

    return True


async def verify_turnstile_token(
    token: Optional[str],
    remote_ip: Optional[str] = None,
) -> bool:
    """
    Verify a Turnstile token with Cloudflare's siteverify API.

    Returns True if:
      - the secret key is not configured (skipped in dev), OR
      - the token passes Cloudflare's verification.

    Returns False if:
      - the secret key IS configured but no token was provided, OR
      - Cloudflare rejects the token, OR
      - the siteverify call fails (network error, timeout, etc.).
    """
    secret_key = settings.turnstile_secret_key
    if not secret_key:
        logger.debug(
            "Turnstile secret key not configured — skipping CAPTCHA validation",
        )
        return True

    if not token:
        logger.warning("Turnstile token missing but secret key is configured")
        return False

    payload: dict[str, str] = {
        "secret": secret_key,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=TURNSTILE_TIMEOUT_SECONDS) as client:
            resp = await client.post(TURNSTILE_SITEVERIFY_URL, data=payload)
            resp.raise_for_status()
            result = resp.json()
            success = result.get("success", False)
            if not success:
                error_codes = result.get("error-codes", [])
                logger.warning(f"Turnstile verification failed: {error_codes}")
            return success
    except Exception:
        logger.exception("Turnstile siteverify request failed")
        return False


class EmailVerificationDAO:
    """DAO for email verification codes (signup and password reset)."""

    def __init__(self, session: Session):
        self.session = session

    def create_signup_verification(
        self,
        email: str,
        code: str,
        password_hash: str,
        name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> EmailVerification:
        """
        Create a signup verification entry.

        If a pending signup already exists for this email, it is overwritten
        (deleted and recreated) so the latest code always wins.

        :param email: The email to verify.
        :param code: The plaintext 6-digit code (will be hashed before storage).
        :param password_hash: The argon2id hash of the user's chosen password.
        :param name: The user's first name.
        :param last_name: The user's last name.
        :return: The created EmailVerification instance.
        """
        # Delete any existing pending signup for this email
        self.delete_by_email_and_purpose(email, "signup")

        verification = EmailVerification(
            email=email,
            code_hash=hash_code(code),
            purpose="signup",
            password_hash=password_hash,
            name=name,
            last_name=last_name,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=SIGNUP_TTL_HOURS),
            attempts=0,
        )
        self.session.add(verification)
        return verification

    def create_password_reset_verification(
        self,
        email: str,
        code: str,
    ) -> EmailVerification:
        """
        Create a password reset verification entry.

        If a pending reset already exists for this email, it is overwritten.

        :param email: The email for the password reset.
        :param code: The plaintext 6-digit code (will be hashed before storage).
        :return: The created EmailVerification instance.
        """
        # Delete any existing pending reset for this email
        self.delete_by_email_and_purpose(email, "password_reset")

        verification = EmailVerification(
            email=email,
            code_hash=hash_code(code),
            purpose="password_reset",
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES),
            attempts=0,
        )
        self.session.add(verification)
        return verification

    def get_pending(
        self,
        email: str,
        purpose: str,
    ) -> Optional[EmailVerification]:
        """
        Get the most recent non-expired verification entry for an email and purpose.

        :param email: The email address.
        :param purpose: "signup" or "password_reset".
        :return: EmailVerification instance or None.
        """
        now = datetime.now(timezone.utc)
        query = (
            select(EmailVerification)
            .where(
                EmailVerification.email == email,
                EmailVerification.purpose == purpose,
                EmailVerification.expires_at > now,
                EmailVerification.attempts < MAX_ATTEMPTS,
            )
            .order_by(EmailVerification.created_at.desc())
            .limit(1)
        )
        return self.session.execute(query).scalars().first()

    def validate_code(
        self,
        email: str,
        code: str,
        purpose: str,
    ) -> Optional[EmailVerification]:
        """
        Validate a verification code.

        Checks hash match, TTL, and attempt count. Increments attempts on
        each call regardless of success. Returns the verification entry on
        success, or None on failure.

        :param email: The email address.
        :param code: The plaintext 6-digit code submitted by the user.
        :param purpose: "signup" or "password_reset".
        :return: EmailVerification instance if valid, None otherwise.
        """
        verification = self.get_pending(email, purpose)
        if verification is None:
            return None

        # Increment attempts regardless of success
        verification.attempts += 1

        # Check if code matches
        if verification.code_hash != hash_code(code):
            return None

        return verification

    def delete(self, verification_id: int) -> None:
        """
        Delete a verification entry by ID.

        :param verification_id: The ID of the entry to delete.
        """
        entry = self.session.get(EmailVerification, verification_id)
        if entry is not None:
            self.session.delete(entry)

    def delete_expired(self) -> int:
        """
        Delete all expired verification entries.

        Intended for periodic cleanup jobs.

        :return: Number of rows deleted.
        """
        now = datetime.now(timezone.utc)
        result = (
            self.session.query(EmailVerification)
            .filter(EmailVerification.expires_at < now)
            .delete()
        )
        return result

    def delete_by_email_and_purpose(self, email: str, purpose: str) -> int:
        """
        Delete all verification entries for an email and purpose.

        :param email: The email address.
        :param purpose: "signup" or "password_reset".
        :return: Number of rows deleted.
        """
        result = (
            self.session.query(EmailVerification)
            .filter(
                EmailVerification.email == email,
                EmailVerification.purpose == purpose,
            )
            .delete()
        )
        return result
