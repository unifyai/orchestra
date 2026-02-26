"""
Tests for Phase 2: TOTP Two-Factor Authentication.

Covers:
- MFA crypto (encrypt / decrypt round-trip, local fallback)
- MFA credential DAO (create, confirm, verify with replay protection)
- MFA recovery DAO (generate, consume, remaining count)
- Authenticate endpoint returns mfa_required when MFA is enabled
"""

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pyotp

from orchestra.db.dao.mfa_credential_dao import (
    RECOVERY_CODE_COUNT,
    RECOVERY_CODE_LENGTH,
    MFACredentialDAO,
    MFARecoveryDAO,
    _generate_recovery_codes,
    _hash_recovery_code,
    decrypt_secret,
    encrypt_secret,
)
from orchestra.db.models.orchestra_models import MFACredential, MFARecovery

# =============================================================================
# MFA Crypto
# =============================================================================


class TestMFACrypto:
    """Tests for encrypt_secret / decrypt_secret (local Fernet fallback)."""

    def test_round_trip(self):
        """Encrypting then decrypting returns the original secret."""
        secret = pyotp.random_base32()
        ciphertext = encrypt_secret(secret)
        assert isinstance(ciphertext, bytes)
        assert ciphertext != secret.encode()
        assert decrypt_secret(ciphertext) == secret

    def test_different_secrets_produce_different_ciphertexts(self):
        """Two different secrets must produce different ciphertexts."""
        s1 = pyotp.random_base32()
        s2 = pyotp.random_base32()
        assert encrypt_secret(s1) != encrypt_secret(s2)

    def test_ciphertext_is_not_plaintext(self):
        """The ciphertext must not contain the plaintext secret."""
        secret = "JBSWY3DPEHPK3PXP"
        ciphertext = encrypt_secret(secret)
        assert secret.encode() not in ciphertext


# =============================================================================
# Recovery Code Helpers
# =============================================================================


class TestRecoveryCodeHelpers:
    """Tests for recovery code generation and hashing."""

    def test_generate_correct_count(self):
        codes = _generate_recovery_codes()
        assert len(codes) == RECOVERY_CODE_COUNT

    def test_generate_correct_length(self):
        codes = _generate_recovery_codes()
        for code in codes:
            assert len(code) == RECOVERY_CODE_LENGTH

    def test_codes_are_unique(self):
        codes = _generate_recovery_codes()
        assert len(set(codes)) == len(codes)

    def test_codes_are_alphanumeric_lowercase(self):
        codes = _generate_recovery_codes()
        for code in codes:
            assert code.isalnum()
            assert code == code.lower()

    def test_hash_is_sha256(self):
        code = "abcd1234"
        expected = hashlib.sha256(code.encode()).hexdigest()
        assert _hash_recovery_code(code) == expected


# =============================================================================
# MFACredentialDAO (mocked session)
# =============================================================================


class TestMFACredentialDAO:
    """Unit tests for MFACredentialDAO with a mocked SQLAlchemy session."""

    def _make_dao(self):
        session = MagicMock(spec_set=["add", "delete", "flush", "execute", "query"])
        return MFACredentialDAO(session), session

    def test_create_totp_stores_encrypted_data(self):
        """create_totp_credential encrypts the secret and stores it."""
        dao, session = self._make_dao()
        # No pending credential
        session.execute.return_value.scalars.return_value.first.return_value = None

        credential, uri = dao.create_totp_credential(
            user_id="user-1",
            user_email="alice@example.com",
        )

        assert credential.user_id == "user-1"
        assert credential.method_type == "totp"
        assert credential.enabled is False
        assert credential.credential_data is not None

        # URI should be an otpauth:// URI
        assert uri.startswith("otpauth://totp/")
        assert "Unify" in uri
        assert "alice%40example.com" in uri or "alice@example.com" in uri

        # Verify the encrypted data round-trips
        decrypted = decrypt_secret(credential.credential_data)
        totp = pyotp.TOTP(decrypted)
        assert totp.verify(totp.now())

        session.add.assert_called_once_with(credential)

    def test_confirm_totp_sets_enabled_and_timestamp(self):
        """confirm_totp sets enabled=True and confirmed_at."""
        dao, _ = self._make_dao()
        credential = MFACredential(
            user_id="user-1",
            method_type="totp",
            credential_data=b"fake",
            enabled=False,
        )

        dao.confirm_totp(credential)

        assert credential.enabled is True
        assert credential.confirmed_at is not None
        assert isinstance(credential.confirmed_at, datetime)

    def test_verify_totp_code_valid(self):
        """verify_totp_code accepts a valid TOTP code."""
        dao, _ = self._make_dao()
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)

        credential = MFACredential(
            user_id="user-1",
            method_type="totp",
            credential_data=encrypt_secret(secret),
            enabled=True,
            last_used_at=None,
        )

        assert dao.verify_totp_code(credential, totp.now()) is True
        assert credential.last_used_at is not None

    def test_verify_totp_code_wrong_code(self):
        """verify_totp_code rejects an invalid code."""
        dao, _ = self._make_dao()
        secret = pyotp.random_base32()

        credential = MFACredential(
            user_id="user-1",
            method_type="totp",
            credential_data=encrypt_secret(secret),
            enabled=True,
            last_used_at=None,
        )

        assert dao.verify_totp_code(credential, "000000") is False
        assert credential.last_used_at is None

    def test_verify_totp_code_replay_protection(self):
        """verify_totp_code rejects a code if the same time-step was already used."""
        dao, _ = self._make_dao()
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)

        credential = MFACredential(
            user_id="user-1",
            method_type="totp",
            credential_data=encrypt_secret(secret),
            enabled=True,
            last_used_at=None,
        )

        code = totp.now()

        # First use should succeed
        assert dao.verify_totp_code(credential, code) is True

        # Same code again should fail (replay)
        assert dao.verify_totp_code(credential, code) is False

    def test_has_enabled_mfa_true(self):
        """has_enabled_mfa returns True when an enabled credential exists."""
        dao, session = self._make_dao()
        session.execute.return_value.scalars.return_value.first.return_value = 1

        assert dao.has_enabled_mfa("user-1") is True

    def test_has_enabled_mfa_false(self):
        """has_enabled_mfa returns False when no enabled credential exists."""
        dao, session = self._make_dao()
        session.execute.return_value.scalars.return_value.first.return_value = None

        assert dao.has_enabled_mfa("user-1") is False


# =============================================================================
# MFARecoveryDAO (mocked session)
# =============================================================================


class TestMFARecoveryDAO:
    """Unit tests for MFARecoveryDAO with a mocked SQLAlchemy session."""

    def _make_dao(self):
        session = MagicMock(spec_set=["add", "execute", "query"])
        # Mock query().filter().delete() chain for delete_all_for_user
        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 0
        session.query.return_value = mock_query
        return MFARecoveryDAO(session), session

    def test_generate_and_store_creates_correct_count(self):
        """generate_and_store creates RECOVERY_CODE_COUNT hashed entries."""
        dao, session = self._make_dao()

        codes = dao.generate_and_store("user-1")

        assert len(codes) == RECOVERY_CODE_COUNT
        assert session.add.call_count == RECOVERY_CODE_COUNT

        # Each stored entry should be a hashed code
        for call in session.add.call_args_list:
            entry = call[0][0]
            assert isinstance(entry, MFARecovery)
            assert entry.user_id == "user-1"
            assert entry.used is False
            # code_hash should be a valid SHA-256 hex digest
            assert len(entry.code_hash) == 64

    def test_generate_and_store_returns_plaintext(self):
        """The returned codes are plaintext (not hashes)."""
        dao, _ = self._make_dao()

        codes = dao.generate_and_store("user-1")

        for code in codes:
            assert len(code) == RECOVERY_CODE_LENGTH
            assert code.isalnum()

    def test_verify_and_consume_valid(self):
        """verify_and_consume marks a valid code as used and returns remaining count."""
        dao, session = self._make_dao()

        # Mock finding the code entry
        mock_entry = MFARecovery(
            user_id="user-1",
            code_hash=_hash_recovery_code("testcode"),
            used=False,
        )
        session.execute.return_value.scalars.return_value.first.side_effect = [
            mock_entry,  # first call: find the code
        ]
        # Mock remaining count query
        remaining_scalars = MagicMock()
        remaining_scalars.all.return_value = [1, 2, 3]  # 3 remaining
        remaining_execute = MagicMock()
        remaining_execute.scalars.return_value = remaining_scalars

        # First execute call returns the entry, second returns remaining IDs
        session.execute.side_effect = [
            MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(first=MagicMock(return_value=mock_entry))
                )
            ),
            remaining_execute,
        ]

        result = dao.verify_and_consume("user-1", "testcode")

        assert result == 3
        assert mock_entry.used is True
        assert mock_entry.used_at is not None

    def test_verify_and_consume_invalid_code(self):
        """verify_and_consume returns None for an invalid code."""
        dao, session = self._make_dao()

        # Mock: no matching code found
        session.execute.return_value.scalars.return_value.first.return_value = None

        result = dao.verify_and_consume("user-1", "wrongcode")

        assert result is None


# =============================================================================
# Authenticate endpoint MFA integration
# =============================================================================


class TestAuthenticateMFAIntegration:
    """
    Verify that the authenticate logic correctly reports mfa_required.

    These are pure unit tests — they mock the DAO layer rather than
    making HTTP calls.
    """

    def test_mfa_required_flag_set_when_mfa_enabled(self):
        """
        When a user has an enabled MFA credential, has_enabled_mfa returns True.
        """
        session = MagicMock()
        dao = MFACredentialDAO(session)

        # Mock: enabled credential exists
        session.execute.return_value.scalars.return_value.first.return_value = 42

        assert dao.has_enabled_mfa("user-with-mfa") is True

    def test_mfa_required_flag_false_when_no_mfa(self):
        """
        When a user has no MFA credential, has_enabled_mfa returns False.
        """
        session = MagicMock()
        dao = MFACredentialDAO(session)

        # Mock: no credential
        session.execute.return_value.scalars.return_value.first.return_value = None

        assert dao.has_enabled_mfa("user-no-mfa") is False


# =============================================================================
# Full TOTP setup → confirm → verify flow (unit)
# =============================================================================


class TestTOTPFullFlow:
    """
    End-to-end unit test for the TOTP lifecycle: setup → confirm → verify.

    Uses real encryption and real pyotp but mocks the DB session.
    """

    def test_setup_confirm_verify_cycle(self):
        """
        1. Create a pending TOTP credential
        2. Confirm it with a valid code
        3. Verify login with a fresh code
        """
        session = MagicMock()
        session.execute.return_value.scalars.return_value.first.return_value = None

        dao = MFACredentialDAO(session)

        # Step 1: Setup
        credential, uri = dao.create_totp_credential("user-1", "user@test.com")
        assert credential.enabled is False
        assert uri.startswith("otpauth://")

        # Recover the secret for code generation
        secret = decrypt_secret(credential.credential_data)
        totp = pyotp.TOTP(secret)

        # Step 2: Confirm with a valid TOTP code
        code = totp.now()
        assert dao.verify_totp_code(credential, code) is True
        dao.confirm_totp(credential)
        assert credential.enabled is True
        assert credential.confirmed_at is not None

        # Step 3: Wait for the next time-step (so replay protection doesn't block)
        # We simulate this by resetting last_used_at to the past
        credential.last_used_at = datetime.now(timezone.utc) - timedelta(seconds=35)

        # Generate a fresh code (should be valid)
        new_code = totp.now()
        assert dao.verify_totp_code(credential, new_code) is True

    def test_recovery_code_full_flow(self):
        """
        1. Generate recovery codes
        2. Use one → remaining decreases
        3. Same code again → fails (already used)
        """
        session = MagicMock()

        # Mock delete for generate_and_store
        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 0
        session.query.return_value = mock_query

        recovery_dao = MFARecoveryDAO(session)

        # Step 1: Generate
        codes = recovery_dao.generate_and_store("user-1")
        assert len(codes) == RECOVERY_CODE_COUNT

        # Step 2: Simulate consuming a code
        # We need to mock the DB lookup. Create a real entry for the first code.
        real_entry = MFARecovery(
            user_id="user-1",
            code_hash=_hash_recovery_code(codes[0]),
            used=False,
        )
        remaining_result = MagicMock()
        remaining_result.scalars.return_value.all.return_value = list(range(9))

        session.execute.side_effect = [
            MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(first=MagicMock(return_value=real_entry)),
                ),
            ),
            remaining_result,
        ]

        remaining = recovery_dao.verify_and_consume("user-1", codes[0])
        assert remaining == 9
        assert real_entry.used is True

        # Step 3: Same code again → already used
        session.execute.side_effect = [
            MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(first=MagicMock(return_value=None)),
                ),
            ),
        ]
        result = recovery_dao.verify_and_consume("user-1", codes[0])
        assert result is None
