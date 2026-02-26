"""
Data Access Object for MFACredential (TOTP / WebAuthn / SMS credentials).

Handles CRUD, TOTP verification with replay protection, recovery-code
generation / consumption via :class:`MFARecoveryDAO`, and MFA credential
encryption (GCP Cloud KMS with local Fernet fallback).
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import string
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import pyotp
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import MFACredential, MFARecovery
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 8  # e.g. "a3f8k2m9"
RECOVERY_CODE_ALPHABET = string.ascii_lowercase + string.digits
TOTP_VALID_WINDOW = 1  # ±30 seconds tolerance
ISSUER_NAME = "Unify"

# GCP KMS configuration
_GCP_PROJECT: str = getattr(settings, "gcp_project", "") or ""
_GCP_LOCATION: str = getattr(settings, "gcp_location", "") or ""
_KMS_KEYRING: str = os.environ.get("MFA_KMS_KEYRING", "mfa")
_KMS_KEY: str = os.environ.get("MFA_KMS_KEY", "mfa-secrets")

# Local fallback key (dev / CI).
_LOCAL_KEY_ENV: Optional[str] = os.environ.get("MFA_ENCRYPTION_KEY")


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------


def _kms_available() -> bool:
    """Return True when GCP KMS can be used."""
    return bool(_GCP_PROJECT and _GCP_LOCATION)


def _get_fernet() -> Fernet:
    """Return a Fernet instance for local encryption."""
    if _LOCAL_KEY_ENV:
        raw = _LOCAL_KEY_ENV.encode()
    else:
        # Deterministic but unique-per-project seed for dev convenience.
        raw = b"orchestra-mfa-dev-key-do-not-use-in-prod"
    # Fernet requires a 32-byte url-safe-base64 key.
    derived = hashlib.sha256(raw).digest()
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
                _GCP_PROJECT,
                _GCP_LOCATION,
                _KMS_KEYRING,
                _KMS_KEY,
            )
            response = client.encrypt(
                request={"name": key_name, "plaintext": payload},
            )
            return response.ciphertext
        except Exception:
            logger.exception("KMS encrypt failed — falling back to local key")

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
                _GCP_PROJECT,
                _GCP_LOCATION,
                _KMS_KEYRING,
                _KMS_KEY,
            )
            response = client.decrypt(
                request={"name": key_name, "ciphertext": ciphertext},
            )
            data = json.loads(response.plaintext.decode())
            return data["secret"]
        except Exception:
            logger.exception("KMS decrypt failed — falling back to local key")

    data = json.loads(_get_fernet().decrypt(ciphertext).decode())
    return data["secret"]


# ---------------------------------------------------------------------------
# Recovery code helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# MFACredentialDAO
# ---------------------------------------------------------------------------


class MFACredentialDAO:
    """DAO for MFA credentials (primarily TOTP)."""

    def __init__(self, session: Session):
        self.session = session

    # --- queries ---

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

    # --- setup ---

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

    # --- verification ---

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

    # --- disable ---

    def delete_credential(self, credential: MFACredential) -> None:
        """Delete an MFA credential."""
        self.session.delete(credential)

    def delete_all_for_user(self, user_id: str) -> int:
        """Delete all MFA credentials for a user."""
        count = (
            self.session.query(MFACredential)
            .filter(MFACredential.user_id == user_id)
            .delete()
        )
        return count


# ---------------------------------------------------------------------------
# MFARecoveryDAO
# ---------------------------------------------------------------------------


class MFARecoveryDAO:
    """DAO for MFA recovery codes."""

    def __init__(self, session: Session):
        self.session = session

    def generate_and_store(self, user_id: str) -> List[str]:
        """
        Generate new recovery codes, delete any existing ones, and store
        the hashed codes.

        :returns: List of plaintext codes (shown to user **once**).
        """
        # Delete existing codes
        self.delete_all_for_user(user_id)

        plaintext_codes = _generate_recovery_codes()
        for code in plaintext_codes:
            entry = MFARecovery(
                user_id=user_id,
                code_hash=_hash_recovery_code(code),
                used=False,
            )
            self.session.add(entry)

        return plaintext_codes

    def verify_and_consume(self, user_id: str, code: str) -> Optional[int]:
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
        # Subtract 1 because the current code is still marked used in the
        # same session but we already set used=True above — SQLAlchemy
        # tracks the change in-session.  Actually the query will already
        # exclude it because we set used=True, so remaining is correct.
        return remaining

    def remaining_count(self, user_id: str) -> int:
        """Return the number of unused recovery codes."""
        query = select(MFARecovery.id).where(
            MFARecovery.user_id == user_id,
            MFARecovery.used.is_(False),
        )
        return len(self.session.execute(query).scalars().all())

    def delete_all_for_user(self, user_id: str) -> int:
        """Delete all recovery codes for a user."""
        count = (
            self.session.query(MFARecovery)
            .filter(MFARecovery.user_id == user_id)
            .delete()
        )
        return count
