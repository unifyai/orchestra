"""
Unit tests for auth utility functions and routines.

These test standalone functions that don't require the FastAPI app or
database — just real code, no HTTP calls.

Sections:
- MFACrypto: encrypt_secret / decrypt_secret round-trip
- RecoveryCodeHelpers: generation and hashing
- TurnstileVerification: verify_turnstile_token behaviour
- TOTPVerification: verify_totp_code correctness and replay protection
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pyotp
import pytest

from orchestra.db.dao.auth_dao import (
    RECOVERY_CODE_COUNT,
    RECOVERY_CODE_LENGTH,
    AuthDAO,
    _generate_recovery_codes,
    _hash_recovery_code,
    decrypt_secret,
    encrypt_secret,
)
from orchestra.db.models.orchestra_models import MFACredential

_MODULE_PATH = "orchestra.db.dao.auth_dao"
_SETTINGS_PATH = f"{_MODULE_PATH}.settings"
_HTTP_CLIENT_PATH = "orchestra.web.api.utils.http_client.get_async_client"


# ═══════════════════════════════════════════════════════════════════════════
# MFA Crypto
# ═══════════════════════════════════════════════════════════════════════════


class TestMFACrypto:
    """encrypt_secret / decrypt_secret (local Fernet fallback)."""

    def test_round_trip(self):
        secret = pyotp.random_base32()
        ciphertext = encrypt_secret(secret)
        assert isinstance(ciphertext, bytes)
        assert ciphertext != secret.encode()
        assert decrypt_secret(ciphertext) == secret

    def test_different_secrets_produce_different_ciphertexts(self):
        s1 = pyotp.random_base32()
        s2 = pyotp.random_base32()
        assert encrypt_secret(s1) != encrypt_secret(s2)

    def test_ciphertext_does_not_contain_plaintext(self):
        secret = "JBSWY3DPEHPK3PXP"
        assert secret.encode() not in encrypt_secret(secret)


# ═══════════════════════════════════════════════════════════════════════════
# Recovery Code Helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestRecoveryCodeHelpers:
    """_generate_recovery_codes and _hash_recovery_code."""

    def test_correct_count(self):
        assert len(_generate_recovery_codes()) == RECOVERY_CODE_COUNT

    def test_correct_length(self):
        for code in _generate_recovery_codes():
            assert len(code) == RECOVERY_CODE_LENGTH

    def test_unique(self):
        codes = _generate_recovery_codes()
        assert len(set(codes)) == len(codes)

    def test_alphanumeric_lowercase(self):
        for code in _generate_recovery_codes():
            assert code.isalnum()
            assert code == code.lower()

    def test_hash_is_sha256(self):
        import hashlib

        code = "abcd1234"
        assert _hash_recovery_code(code) == hashlib.sha256(code.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# Turnstile Verification
# ═══════════════════════════════════════════════════════════════════════════


class TestTurnstileVerification:
    """verify_turnstile_token — mocked HTTP + settings."""

    @pytest.mark.anyio
    async def test_skips_when_no_secret_key(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        with patch(_SETTINGS_PATH) as ms:
            ms.turnstile_secret_key = None
            assert await verify_turnstile_token("any-token") is True

    @pytest.mark.anyio
    async def test_rejects_missing_token_when_configured(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        with patch(_SETTINGS_PATH) as ms:
            ms.turnstile_secret_key = "test-secret"
            assert await verify_turnstile_token(None) is False

    @pytest.mark.anyio
    async def test_rejects_empty_token_when_configured(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        with patch(_SETTINGS_PATH) as ms:
            ms.turnstile_secret_key = "test-secret"
            assert await verify_turnstile_token("") is False

    @pytest.mark.anyio
    async def test_returns_true_on_cloudflare_success(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True}
        mock_response.raise_for_status = MagicMock()

        with (
            patch(_SETTINGS_PATH) as ms,
            patch(_HTTP_CLIENT_PATH) as get_async_client,
        ):
            ms.turnstile_secret_key = "test-secret"
            mc = AsyncMock()
            mc.post.return_value = mock_response
            get_async_client.return_value = mc

            assert await verify_turnstile_token("valid-token") is True

        data = mc.post.call_args[1]["data"]
        assert data["secret"] == "test-secret"
        assert data["response"] == "valid-token"

    @pytest.mark.anyio
    async def test_returns_false_on_cloudflare_failure(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": False,
            "error-codes": ["invalid-input-response"],
        }
        mock_response.raise_for_status = MagicMock()

        with (
            patch(_SETTINGS_PATH) as ms,
            patch(_HTTP_CLIENT_PATH) as get_async_client,
        ):
            ms.turnstile_secret_key = "test-secret"
            mc = AsyncMock()
            mc.post.return_value = mock_response
            get_async_client.return_value = mc

            assert await verify_turnstile_token("bad-token") is False

    @pytest.mark.anyio
    async def test_returns_false_on_network_error(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        with (
            patch(_SETTINGS_PATH) as ms,
            patch(_HTTP_CLIENT_PATH) as get_async_client,
        ):
            ms.turnstile_secret_key = "test-secret"
            mc = AsyncMock()
            mc.post.side_effect = httpx.ConnectError("Connection refused")
            get_async_client.return_value = mc

            assert await verify_turnstile_token("valid-token") is False

    @pytest.mark.anyio
    async def test_forwards_remote_ip(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True}
        mock_response.raise_for_status = MagicMock()

        with (
            patch(_SETTINGS_PATH) as ms,
            patch(_HTTP_CLIENT_PATH) as get_async_client,
        ):
            ms.turnstile_secret_key = "test-secret"
            mc = AsyncMock()
            mc.post.return_value = mock_response
            get_async_client.return_value = mc

            assert (
                await verify_turnstile_token("valid-token", remote_ip="1.2.3.4") is True
            )

        assert mc.post.call_args[1]["data"]["remoteip"] == "1.2.3.4"

    @pytest.mark.anyio
    async def test_omits_remote_ip_when_none(self):
        from orchestra.db.dao.auth_dao import verify_turnstile_token

        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True}
        mock_response.raise_for_status = MagicMock()

        with (
            patch(_SETTINGS_PATH) as ms,
            patch(_HTTP_CLIENT_PATH) as get_async_client,
        ):
            ms.turnstile_secret_key = "test-secret"
            mc = AsyncMock()
            mc.post.return_value = mock_response
            get_async_client.return_value = mc

            assert await verify_turnstile_token("valid-token", remote_ip=None) is True

        assert "remoteip" not in mc.post.call_args[1]["data"]


# ═══════════════════════════════════════════════════════════════════════════
# TOTP Verification
# ═══════════════════════════════════════════════════════════════════════════


class TestTOTPVerification:
    """AuthDAO.verify_totp_code — real crypto, mocked session."""

    def _make_credential(self, secret: str) -> MFACredential:
        return MFACredential(
            user_id="user-1",
            method_type="totp",
            credential_data=encrypt_secret(secret),
            enabled=True,
            last_used_at=None,
        )

    def test_accepts_valid_code(self):
        session = MagicMock()
        dao = AuthDAO(session)
        secret = pyotp.random_base32()
        cred = self._make_credential(secret)

        assert dao.verify_totp_code(cred, pyotp.TOTP(secret).now()) is True
        assert cred.last_used_at is not None

    def test_rejects_invalid_code(self):
        session = MagicMock()
        dao = AuthDAO(session)
        cred = self._make_credential(pyotp.random_base32())

        assert dao.verify_totp_code(cred, "000000") is False
        assert cred.last_used_at is None

    def test_replay_protection(self):
        session = MagicMock()
        dao = AuthDAO(session)
        secret = pyotp.random_base32()
        cred = self._make_credential(secret)
        code = pyotp.TOTP(secret).now()

        assert dao.verify_totp_code(cred, code) is True
        assert dao.verify_totp_code(cred, code) is False
