"""
Consolidated Authentication DAO.

Merges five formerly separate DAOs into a single cohesive class:

  - AccountDAO         → OAuth provider account linking
  - EmailAccountDAO    → Email/password credential management
  - EmailVerificationDAO → Signup & password-reset verification codes
  - MFACredentialDAO   → TOTP credential lifecycle
  - MFARecoveryDAO     → MFA recovery-code management

Module-level helpers (auth-related utilities, not DAO methods):

  - Verification code generation & hashing
  - JWT verification-token signing & decoding
  - Cloudflare Turnstile CAPTCHA verification
  - Disposable-email detection
  - User-Agent heuristics for bot detection
  - MFA secret encryption (GCP Cloud KMS with local Fernet fallback)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import jwt
import pyotp
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from disposable_email_domains import blocklist as disposable_blocklist
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Account,
    EmailAccount,
    EmailVerification,
    MFACredential,
    MFARecovery,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# =============================================================================
# Constants — Verification
# =============================================================================

SIGNUP_TTL_HOURS = 1
PASSWORD_RESET_TTL_MINUTES = 10
MAX_ATTEMPTS = 5
VERIFY_TOKEN_TTL_MINUTES = 5

# =============================================================================
# Constants — Turnstile CAPTCHA
# =============================================================================

TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT_SECONDS = 5

# =============================================================================
# Constants — MFA
# =============================================================================

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 8  # e.g. "a3f8k2m9"
RECOVERY_CODE_ALPHABET = string.ascii_lowercase + string.digits
TOTP_VALID_WINDOW = 1  # ±30 seconds tolerance
ISSUER_NAME = "Unify"

# GCP KMS configuration
GCP_PROJECT: str = getattr(settings, "gcp_project")
GCP_LOCATION: str = getattr(settings, "gcp_location")
KMS_KEYRING: str = settings.mfa_kms_keyring
KMS_KEY: str = settings.mfa_kms_key

# Local fallback key (dev / CI).
MFA_ENCRYPTION_KEY: Optional[str] = settings.mfa_encryption_key

# =============================================================================
# JWT verification-token helpers
# =============================================================================


def _get_verify_token_secret() -> str:
    """
    Get the JWT signing secret for verification tokens.

    Uses a dedicated secret if configured, otherwise derives one from
    the admin key via HKDF to ensure key separation.
    """
    if settings.email_verify_token_secret:
        return settings.email_verify_token_secret

    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY")
    if not admin_key:
        raise RuntimeError(
            "EMAIL_VERIFY_TOKEN_SECRET or ORCHESTRA_ADMIN_KEY must be set",
        )
    derived = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=None,
        info=b"email-verify-token",
    ).derive(admin_key.encode())
    return derived.hex()


def sign_verification_token(email: str, purpose: str) -> tuple[str, str]:
    """
    Create a short-lived JWT proving the email code was verified.

    :param email: The verified email address.
    :param purpose: ``"signup"`` or ``"password_reset"``.
    :return: ``(encoded_jwt, jti)`` — the token string and its unique ID.
    """
    jti = str(uuid.uuid4())
    payload = {
        "sub": email,
        "purpose": purpose,
        "jti": jti,
        "exp": datetime.now(tz=timezone.utc)
        + timedelta(minutes=VERIFY_TOKEN_TTL_MINUTES),
        "iat": datetime.now(tz=timezone.utc),
    }
    token = jwt.encode(payload, _get_verify_token_secret(), algorithm="HS256")
    return token, jti


def decode_verification_token(token: str, expected_purpose: str) -> tuple[str, str]:
    """
    Decode and validate a verification token.

    :param token: The JWT string.
    :param expected_purpose: Required purpose (``"signup"`` or ``"password_reset"``).
    :return: ``(email, jti)`` — the email and the token's unique ID.
    :raises HTTPException: On expired, invalid, or wrong-purpose tokens.
    """
    try:
        payload = jwt.decode(token, _get_verify_token_secret(), algorithms=["HS256"])
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

    return payload["sub"], payload.get("jti", "")


# =============================================================================
# Anti-abuse utilities
# =============================================================================

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
        from orchestra.web.api.utils.http_client import get_async_client

        client = get_async_client()
        resp = await client.post(
            TURNSTILE_SITEVERIFY_URL,
            data=payload,
            timeout=TURNSTILE_TIMEOUT_SECONDS,
        )
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


# =============================================================================
# MFA encryption helpers
# =============================================================================


def _kms_available() -> bool:
    """Return True when GCP KMS can be used."""
    return bool(GCP_PROJECT and GCP_LOCATION)


def _get_fernet() -> Fernet:
    """Return a Fernet instance for local encryption using HKDF key derivation."""
    if MFA_ENCRYPTION_KEY:
        raw = MFA_ENCRYPTION_KEY.encode()
    else:
        admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY")
        if not admin_key:
            raise RuntimeError(
                "MFA_ENCRYPTION_KEY or ORCHESTRA_ADMIN_KEY must be set "
                "for MFA credential encryption",
            )
        raw = admin_key.encode()

    derived = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=b"mfa-local-fernet",
        info=b"encryption-key",
    ).derive(raw)
    key = base64.urlsafe_b64encode(derived)
    return Fernet(key)


def encrypt_secret(plaintext: str) -> bytes:
    """
    Encrypt an MFA secret (e.g. a TOTP base32 string).

    In production (GCP KMS configured) the plaintext is encrypted via
    Cloud KMS.  Otherwise a local Fernet key is used.
    """
    payload = json.dumps({"secret": plaintext}).encode()

    if _kms_available():
        try:
            from google.cloud import kms  # lazy import

            client = kms.KeyManagementServiceClient()
            key_name = client.crypto_key_path(
                GCP_PROJECT,
                GCP_LOCATION,
                KMS_KEYRING,
                KMS_KEY,
            )
            response = client.encrypt(
                request={"name": key_name, "plaintext": payload},
            )
            return response.ciphertext
        except Exception:
            logger.exception("KMS encrypt failed")
            if (
                settings.environment not in ("dev", "test", "pytest")
                and not settings.is_staging
            ):
                raise

    return _get_fernet().encrypt(payload)


def decrypt_secret(ciphertext: bytes) -> str:
    """
    Decrypt an MFA secret previously encrypted with ``encrypt_secret``.
    """
    if _kms_available():
        try:
            from google.cloud import kms  # lazy import

            client = kms.KeyManagementServiceClient()
            key_name = client.crypto_key_path(
                GCP_PROJECT,
                GCP_LOCATION,
                KMS_KEYRING,
                KMS_KEY,
            )
            response = client.decrypt(
                request={"name": key_name, "ciphertext": ciphertext},
            )
            data = json.loads(response.plaintext.decode())
            return data["secret"]
        except Exception:
            logger.exception("KMS decrypt failed")
            if (
                settings.environment not in ("dev", "test", "pytest")
                and not settings.is_staging
            ):
                raise

    data = json.loads(_get_fernet().decrypt(ciphertext).decode())
    return data["secret"]


# =============================================================================
# MFA recovery-code helpers
# =============================================================================


def _hash_recovery_code(code: str) -> str:
    """SHA-256 hash a recovery code."""
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_recovery_codes() -> List[str]:
    """Generate ``RECOVERY_CODE_COUNT`` random recovery codes."""
    return [
        "".join(
            secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH)
        )
        for _ in range(RECOVERY_CODE_COUNT)
    ]


# =============================================================================
# AuthDAO
# =============================================================================


class AuthDAO:
    """
    Consolidated DAO for all authentication operations.

    Sections:
      1. OAuth provider accounts
      2. Email/password credentials
      3. Email verification codes (signup & password reset)
      4. MFA credentials (TOTP)
      5. MFA recovery codes
      6. Auth provider helpers
    """

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # 1. OAuth Provider Accounts  (formerly AccountDAO)
    # =========================================================================

    def create_oauth_account(  # noqa: WPS211
        self,
        user_id: str,
        provider: str,
        provider_type: str,
        provider_account_id: str,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """Create an OAuth provider account link for a user."""
        self.session.add(
            Account(
                user_id=user_id,
                provider=provider,
                provider_type=provider_type,
                provider_account_id=provider_account_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
            ),
        )

    def filter_oauth_accounts(
        self,
        id: Optional[str] = None,
        user_id: Optional[str] = None,
        provider: Optional[str] = None,
        provider_account_id: Optional[str] = None,
    ) -> List[Account]:
        """Filter OAuth provider accounts by criteria."""
        query = select(Account)
        if id:
            query = query.where(Account.id == id)
        if user_id:
            query = query.where(Account.user_id == user_id)
        if provider:
            query = query.where(Account.provider == provider)
        if provider_account_id:
            query = query.where(Account.provider_account_id == provider_account_id)
        rows = self.session.execute(query)
        return rows.fetchall()

    def delete_oauth_account(self, id: str):
        """Delete an OAuth provider account by ID."""
        try:
            account = self.session.query(Account).filter_by(id=id).one()
            self.session.delete(account)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise ValueError

    # =========================================================================
    # 2. Email/Password Credentials  (formerly EmailAccountDAO)
    # =========================================================================

    def create_email_credentials(
        self,
        user_id: str,
        password_hash: str,
        email_verified: bool = True,
    ) -> EmailAccount:
        """
        Create an EmailAccount for a user.

        :param user_id: The user's ID (FK to User.id).
        :param password_hash: The argon2id password hash.
        :param email_verified: Whether the email is verified (default True).
        :return: The created EmailAccount instance.
        """
        email_account = EmailAccount(
            user_id=user_id,
            password_hash=password_hash,
            email_verified=email_verified,
        )
        self.session.add(email_account)
        return email_account

    def get_email_credentials(self, user_id: str) -> Optional[EmailAccount]:
        """
        Get an EmailAccount by user ID.

        :param user_id: The user's ID.
        :return: EmailAccount instance or None.
        """
        query = select(EmailAccount).where(EmailAccount.user_id == user_id)
        return self.session.execute(query).scalars().first()

    def update_password(
        self,
        user_id: str,
        new_password_hash: str,
    ) -> Optional[EmailAccount]:
        """
        Update the password hash and set password_changed_at for session invalidation.

        :param user_id: The user's ID.
        :param new_password_hash: The new argon2id password hash.
        :return: The updated EmailAccount, or None if not found.
        """
        email_account = self.get_email_credentials(user_id)
        if email_account is None:
            return None
        email_account.password_hash = new_password_hash
        email_account.password_changed_at = datetime.now(timezone.utc)
        return email_account

    def delete_email_credentials(self, user_id: str) -> bool:
        """
        Delete an EmailAccount by user ID.

        :param user_id: The user's ID.
        :return: True if deleted, False if not found.
        """
        email_account = self.get_email_credentials(user_id)
        if email_account is None:
            return False
        self.session.delete(email_account)
        return True

    # =========================================================================
    # 3. Email Verification Codes  (formerly EmailVerificationDAO)
    # =========================================================================

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
        self.delete_verifications_by_email_and_purpose(email, "signup")

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
        self.delete_verifications_by_email_and_purpose(email, "password_reset")

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

    def get_pending_verification(
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

    def validate_verification_code(
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
        verification = self.get_pending_verification(email, purpose)
        if verification is None:
            return None

        # Increment attempts regardless of success
        verification.attempts += 1

        if not hmac.compare_digest(verification.code_hash, hash_code(code)):
            return None

        return verification

    def delete_verification(self, verification_id: int) -> None:
        """
        Delete a verification entry by ID.

        :param verification_id: The ID of the entry to delete.
        """
        entry = self.session.get(EmailVerification, verification_id)
        if entry is not None:
            self.session.delete(entry)

    def delete_expired_verifications(self) -> int:
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

    def delete_verifications_by_email_and_purpose(
        self,
        email: str,
        purpose: str,
    ) -> int:
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

    # =========================================================================
    # 4. MFA Credentials — TOTP  (formerly MFACredentialDAO)
    # =========================================================================

    def get_enabled_totp(self, user_id: str) -> Optional[MFACredential]:
        """
        Return the user's enabled TOTP credential, or ``None``.
        """
        query = select(MFACredential).where(
            MFACredential.user_id == user_id,
            MFACredential.method_type == "totp",
            MFACredential.enabled.is_(True),
        )
        return self.session.execute(query).scalars().first()

    def get_pending_totp(self, user_id: str) -> Optional[MFACredential]:
        """
        Return the user's pending (not yet confirmed) TOTP credential.
        """
        query = select(MFACredential).where(
            MFACredential.user_id == user_id,
            MFACredential.method_type == "totp",
            MFACredential.enabled.is_(False),
        )
        return self.session.execute(query).scalars().first()

    def has_enabled_mfa(self, user_id: str) -> bool:
        """
        Return ``True`` if the user has *any* enabled MFA credential.
        """
        query = (
            select(MFACredential.id)
            .where(
                MFACredential.user_id == user_id,
                MFACredential.enabled.is_(True),
            )
            .limit(1)
        )
        return self.session.execute(query).scalars().first() is not None

    def create_totp_credential(
        self,
        user_id: str,
        user_email: str,
    ) -> Tuple[MFACredential, str]:
        """
        Generate a new TOTP secret, encrypt it, and store as a pending
        (unconfirmed) credential.  Any existing pending TOTP credential
        for the user is deleted first.

        :returns: ``(credential, provisioning_uri)``
        """
        # Remove any existing pending setup
        pending = self.get_pending_totp(user_id)
        if pending:
            self.session.delete(pending)
            self.session.flush()

        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        provisioning_uri = totp.provisioning_uri(
            name=user_email,
            issuer_name=ISSUER_NAME,
        )

        credential = MFACredential(
            user_id=user_id,
            method_type="totp",
            credential_data=encrypt_secret(secret),
            enabled=False,
        )
        self.session.add(credential)
        return credential, provisioning_uri

    def confirm_totp(self, credential: MFACredential) -> None:
        """
        Mark a TOTP credential as enabled and set ``confirmed_at``.
        """
        credential.enabled = True
        credential.confirmed_at = datetime.now(timezone.utc)

    def verify_totp_code(
        self,
        credential: MFACredential,
        code: str,
    ) -> bool:
        """
        Verify a TOTP code with replay protection.

        The code is accepted if it matches the current or adjacent time-step
        (``valid_window=1`` → ±30 s) **and** the current time-step has not
        already been used (replay protection via ``last_used_at``).

        On success, ``credential.last_used_at`` is updated.

        :returns: ``True`` if the code is valid and not replayed.
        """
        secret = decrypt_secret(credential.credential_data)
        totp = pyotp.TOTP(secret)

        if not totp.verify(code, valid_window=TOTP_VALID_WINDOW):
            return False

        # Replay protection
        now = datetime.now(timezone.utc)
        current_timestep = totp.timecode(now)
        if credential.last_used_at:
            last_timestep = totp.timecode(credential.last_used_at)
            if current_timestep <= last_timestep:
                return False

        credential.last_used_at = now
        return True

    def delete_mfa_credential(self, credential: MFACredential) -> None:
        """Delete an MFA credential."""
        self.session.delete(credential)

    def delete_all_mfa_for_user(self, user_id: str) -> int:
        """Delete all MFA credentials for a user."""
        count = (
            self.session.query(MFACredential)
            .filter(MFACredential.user_id == user_id)
            .delete()
        )
        return count

    # =========================================================================
    # 5. MFA Recovery Codes  (formerly MFARecoveryDAO)
    # =========================================================================

    def generate_recovery_codes(self, user_id: str) -> List[str]:
        """
        Generate new recovery codes, delete any existing ones, and store
        the hashed codes.

        :returns: List of plaintext codes (shown to user **once**).
        """
        # Delete existing codes
        self.delete_all_recovery_codes(user_id)

        plaintext_codes = _generate_recovery_codes()
        for code in plaintext_codes:
            entry = MFARecovery(
                user_id=user_id,
                code_hash=_hash_recovery_code(code),
                used=False,
            )
            self.session.add(entry)

        return plaintext_codes

    def verify_recovery_code(self, user_id: str, code: str) -> Optional[int]:
        """
        Verify a recovery code and mark it as used.

        :returns: Number of remaining unused codes, or ``None`` if the code
                  is invalid.
        """
        code_hash = _hash_recovery_code(code)
        query = select(MFARecovery).where(
            MFARecovery.user_id == user_id,
            MFARecovery.code_hash == code_hash,
            MFARecovery.used.is_(False),
        )
        entry = self.session.execute(query).scalars().first()
        if entry is None:
            return None

        entry.used = True
        entry.used_at = datetime.now(timezone.utc)

        # Count remaining unused codes
        remaining_query = select(MFARecovery.id).where(
            MFARecovery.user_id == user_id,
            MFARecovery.used.is_(False),
        )
        remaining = len(
            self.session.execute(remaining_query).scalars().all(),
        )
        return remaining

    def recovery_codes_remaining(self, user_id: str) -> int:
        """Return the number of unused recovery codes."""
        query = select(MFARecovery.id).where(
            MFARecovery.user_id == user_id,
            MFARecovery.used.is_(False),
        )
        return len(self.session.execute(query).scalars().all())

    def delete_all_recovery_codes(self, user_id: str) -> int:
        """Delete all recovery codes for a user."""
        count = (
            self.session.query(MFARecovery)
            .filter(MFARecovery.user_id == user_id)
            .delete()
        )
        return count

    # =========================================================================
    # 6. Auth Provider Helpers  (formerly on UserDAO)
    # =========================================================================

    def get_linked_providers(self, user_id: str) -> List[str]:
        """
        Get the list of auth providers linked to a user.

        Returns provider names from OAuth accounts (e.g. "google", "github")
        and "email" if the user has an EmailAccount.

        :param user_id: The user's ID.
        :return: List of provider name strings.
        """
        providers = []
        # Check OAuth providers
        accounts = self.filter_oauth_accounts(user_id=user_id)
        for row in accounts:
            account = row[0] if hasattr(row, "__getitem__") else row
            providers.append(account.provider)
        # Check email/password
        if self.get_email_credentials(user_id):
            providers.append("email")
        return providers
