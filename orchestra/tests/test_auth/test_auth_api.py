"""
Black-box API tests for all authentication endpoints.

Every test hits the real FastAPI app with a real database.  Only external
services (email sending, Cloudflare Turnstile) are mocked.  Tests are
grouped into classes by feature area so related behaviour lives together.

Sections:
- Registration (signup validation, disposable emails, duplicates)
- EmailVerification (code validation, expiry, max attempts)
- Authentication (login, wrong password, OAuth-only, MFA flag)
- ForgotResetPassword (forgot, reset, enumeration resistance)
- ChangePassword (authenticated password change)
- SetPassword (OAuth user adds email/password)
- ResendVerification (cooldown, no-pending)
- ProvidersForEmail (email, OAuth, nonexistent)
- EmailCredentials (admin credential lookup)
- OAuthAccountLinking (link/unlink provider accounts)
- Captcha (registration + forgot-password CAPTCHA)
- MFASetup, MFAConfirm, MFAStatus, MFADisable
- MFARecoveryCodes (regeneration)
- MFALoginVerify (admin TOTP + recovery verify)
- MFAStatusByEmail (admin lookup)
- MFAEnforcementStatus (org enforcement check)
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.auth_dao import AuthDAO, hash_code
from orchestra.db.models.orchestra_models import EmailVerification

from .conftest import (
    ADMIN_HEADERS,
    EMAIL_PATCH_TARGET,
    TURNSTILE_PATCH_TARGET,
    advance_totp_window,
    authenticate,
    create_oauth_user,
    forgot_password,
    register,
    register_and_verify,
    setup_and_confirm_mfa,
    verify,
    verify_code,
)

# ---------------------------------------------------------------------------
# Auto-mock Turnstile CAPTCHA for every test in this module
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_turnstile():
    with patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mock:
        mock.return_value = True
        yield mock


# ═══════════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════════


class TestRegistration:
    """POST /admin/auth/register."""

    @pytest.mark.anyio
    async def test_happy_path(self, client: AsyncClient, dbsession: Session):
        email = "reg_happy@example.com"
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            resp = await register(client, email)

        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == email
        assert data["requires_verification"] is True

        dao = AuthDAO(dbsession)
        entry = dao.get_pending_verification(email, "signup")
        assert entry is not None
        assert entry.purpose == "signup"
        assert entry.password_hash is not None
        assert entry.name == "Test"
        assert entry.last_name == "User"
        assert entry.attempts == 0

    @pytest.mark.anyio
    async def test_disposable_email_rejected(self, client: AsyncClient):
        resp = await register(client, "test@mailinator.com")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "disposable_email"

    @pytest.mark.anyio
    async def test_duplicate_email(self, client: AsyncClient, dbsession: Session):
        email = "dup_reg@example.com"
        await client.post(
            "/v0/admin/user",
            json={"email": email},
            headers=ADMIN_HEADERS,
        )
        resp = await register(client, email)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "email_exists"

    @pytest.mark.anyio
    async def test_password_too_short(self, client: AsyncClient):
        resp = await client.post(
            "/v0/admin/auth/register",
            json={"email": "short_pw@example.com", "password": "short", "name": "X"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_invalid_email_format(self, client: AsyncClient):
        resp = await client.post(
            "/v0/admin/auth/register",
            json={"email": "not-an-email", "password": "secureP@ss1"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_overwrites_pending_signup(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "overwrite_pending@example.com"
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await register(client, email, password="firstPassword1!")

        dao = AuthDAO(dbsession)
        first_hash = dao.get_pending_verification(email, "signup").password_hash

        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await register(client, email, password="secondPassword2!")

        entry = dao.get_pending_verification(email, "signup")
        assert entry.password_hash != first_hash
        assert entry.attempts == 0

    @pytest.mark.anyio
    async def test_email_case_insensitive(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            resp = await register(client, "CaseTest@Example.COM")

        assert resp.status_code == 200
        assert resp.json()["email"] == "casetest@example.com"

        dao = AuthDAO(dbsession)
        assert (
            dao.get_pending_verification("casetest@example.com", "signup") is not None
        )

    @pytest.mark.anyio
    async def test_whitespace_trimmed(self, client: AsyncClient):
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            resp = await register(client, "  whitespace@example.com  ")

        assert resp.status_code == 200
        assert resp.json()["email"] == "whitespace@example.com"


# ═══════════════════════════════════════════════════════════════════════════
# Email Verification
# ═══════════════════════════════════════════════════════════════════════════


class TestEmailVerification:
    """POST /admin/auth/verify-code + /admin/auth/create-user."""

    @pytest.mark.anyio
    async def test_happy_path(self, client: AsyncClient, dbsession: Session):
        email = "verify_happy@example.com"
        user_id, _ = await register_and_verify(client, dbsession, email)

        from orchestra.db.dao.user_dao import UserDAO

        user = UserDAO(dbsession).filter(email=email)[0][0]

        ea = AuthDAO(dbsession).get_email_credentials(user.id)
        assert ea is not None
        assert ea.email_verified is True

        # Verification entry consumed
        assert AuthDAO(dbsession).get_pending_verification(email, "signup") is None

    @pytest.mark.anyio
    async def test_wrong_code(self, client: AsyncClient, dbsession: Session):
        email = "verify_wrong@example.com"
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await register(client, email)

        resp = await verify(client, email, "000000")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_code"

        entry = AuthDAO(dbsession).get_pending_verification(email, "signup")
        assert entry.attempts == 1

    @pytest.mark.anyio
    async def test_max_attempts(self, client: AsyncClient, dbsession: Session):
        email = "verify_max@example.com"
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await register(client, email)

        for _ in range(5):
            await verify(client, email, "000000")

        entry = (
            dbsession.query(EmailVerification)
            .filter(
                EmailVerification.email == email,
                EmailVerification.purpose == "signup",
            )
            .first()
        )
        assert entry.attempts >= 5

        entry.code_hash = hash_code("654321")
        dbsession.flush()
        resp = await verify(client, email, "654321")
        assert resp.status_code == 429

    @pytest.mark.anyio
    async def test_expired_code(self, client: AsyncClient, dbsession: Session):
        email = "verify_expired@example.com"
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await register(client, email)

        entry = (
            dbsession.query(EmailVerification)
            .filter(
                EmailVerification.email == email,
                EmailVerification.purpose == "signup",
            )
            .first()
        )
        entry.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        entry.code_hash = hash_code("123456")
        dbsession.flush()

        resp = await verify(client, email, "123456")
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_no_pending_entry(self, client: AsyncClient):
        resp = await verify(client, "ghost@example.com", "123456")
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_creates_api_key(self, client: AsyncClient, dbsession: Session):
        email = "verify_key@example.com"
        await register_and_verify(client, dbsession, email)

        from orchestra.db.dao.api_key_dao import ApiKeyDAO
        from orchestra.db.dao.user_dao import UserDAO

        user = UserDAO(dbsession).filter(email=email)[0][0]
        keys = ApiKeyDAO(dbsession).filter(user_id=user.id)
        assert len(keys) > 0

    @pytest.mark.anyio
    async def test_code_cannot_be_reused(self, client: AsyncClient, dbsession: Session):
        email = "code_reuse@example.com"
        await register_and_verify(client, dbsession, email)
        resp = await verify(client, email, "123456")
        assert resp.status_code in (400, 409)


# ═══════════════════════════════════════════════════════════════════════════
# Authentication
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthentication:
    """POST /admin/auth/authenticate."""

    @pytest.mark.anyio
    async def test_happy_path(self, client: AsyncClient, dbsession: Session):
        email = "auth_ok@example.com"
        pw = "correctPw@1"
        await register_and_verify(client, dbsession, email, password=pw)

        resp = await authenticate(client, email, pw)
        assert resp.status_code == 200
        assert resp.json()["email"] == email
        assert resp.json()["mfa_required"] is False

    @pytest.mark.anyio
    async def test_wrong_password(self, client: AsyncClient, dbsession: Session):
        email = "auth_wrongpw@example.com"
        await register_and_verify(client, dbsession, email, password="correctPw@1")
        resp = await authenticate(client, email, "wrongPw@1")
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "invalid_credentials"

    @pytest.mark.anyio
    async def test_nonexistent_user(self, client: AsyncClient):
        resp = await authenticate(client, "ghost@example.com", "anyPw@1")
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "invalid_credentials"

    @pytest.mark.anyio
    async def test_oauth_only_user(self, client: AsyncClient, dbsession: Session):
        email = "auth_oauth@example.com"
        await create_oauth_user(client, dbsession, email, provider="google")

        resp = await authenticate(client, email, "anyPw@1")
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["error"] == "no_email_account"
        assert "google" in detail["providers"]

    @pytest.mark.anyio
    async def test_unverified_email(self, client: AsyncClient, dbsession: Session):
        email = "auth_unverified@example.com"
        await register_and_verify(client, dbsession, email)

        from orchestra.db.dao.user_dao import UserDAO

        user = UserDAO(dbsession).filter(email=email)[0][0]
        ea = AuthDAO(dbsession).get_email_credentials(user.id)
        ea.email_verified = False
        dbsession.flush()

        resp = await authenticate(client, email, "secureP@ss1")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "email_not_verified"

    @pytest.mark.anyio
    async def test_email_case_insensitive(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "auth_case@example.com"
        pw = "secureP@ss1"
        await register_and_verify(client, dbsession, email, password=pw)
        resp = await authenticate(client, "Auth_Case@Example.COM", pw)
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_password_hash_not_exposed(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "no_hash@example.com"
        pw = "secureP@ss1"
        await register_and_verify(client, dbsession, email, password=pw)

        resp = await authenticate(client, email, pw)
        assert "$argon2" not in str(resp.json())

    @pytest.mark.anyio
    async def test_mfa_required_when_enabled(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "auth_mfa@example.com"
        pw = "secureP@ss1"
        uid, hdrs = await register_and_verify(client, dbsession, email, password=pw)
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        resp = await authenticate(client, email, pw)
        assert resp.status_code == 200
        assert resp.json()["mfa_required"] is True

    @pytest.mark.anyio
    async def test_mfa_not_required_when_disabled(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "auth_nomfa@example.com"
        pw = "secureP@ss1"
        await register_and_verify(client, dbsession, email, password=pw)

        resp = await authenticate(client, email, pw)
        assert resp.status_code == 200
        assert resp.json()["mfa_required"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Forgot / Reset Password
# ═══════════════════════════════════════════════════════════════════════════


class TestForgotResetPassword:
    """POST /admin/auth/forgot-password, /admin/auth/reset-password."""

    @pytest.mark.anyio
    async def test_forgot_happy_path(self, client: AsyncClient, dbsession: Session):
        email = "forgot_ok@example.com"
        await register_and_verify(client, dbsession, email)

        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            resp = await forgot_password(client, email)

        assert resp.status_code == 200
        entry = AuthDAO(dbsession).get_pending_verification(email, "password_reset")
        assert entry is not None
        assert entry.purpose == "password_reset"

    @pytest.mark.anyio
    async def test_forgot_nonexistent_email(self, client: AsyncClient):
        """Returns 200 even for unknown emails (enumeration resistance)."""
        resp = await forgot_password(client, "nobody@example.com")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_forgot_oauth_only(self, client: AsyncClient, dbsession: Session):
        """Returns 200 for OAuth-only users but creates no verification entry."""
        email = "forgot_oauth@example.com"
        await client.post(
            "/v0/admin/user",
            json={"email": email},
            headers=ADMIN_HEADERS,
        )

        resp = await forgot_password(client, email)
        assert resp.status_code == 200
        assert (
            AuthDAO(dbsession).get_pending_verification(email, "password_reset") is None
        )

    @pytest.mark.anyio
    async def test_reset_happy_path(self, client: AsyncClient, dbsession: Session):
        email = "reset_ok@example.com"
        old_pw, new_pw = "oldPw@123", "newPw@456"
        await register_and_verify(client, dbsession, email, password=old_pw)

        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await forgot_password(client, email)

        entry = (
            dbsession.query(EmailVerification)
            .filter(
                EmailVerification.email == email,
                EmailVerification.purpose == "password_reset",
            )
            .first()
        )
        code = "789012"
        entry.code_hash = hash_code(code)
        dbsession.flush()

        vr = await verify_code(client, email, code, purpose="password_reset")
        assert vr.status_code == 200
        token = vr.json()["token"]

        resp = await client.post(
            "/v0/admin/auth/reset-password",
            json={"token": token, "new_password": new_pw},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        assert (await authenticate(client, email, old_pw)).status_code == 401
        assert (await authenticate(client, email, new_pw)).status_code == 200

        from orchestra.db.dao.user_dao import UserDAO

        user = UserDAO(dbsession).filter(email=email)[0][0]
        ea = AuthDAO(dbsession).get_email_credentials(user.id)
        assert ea.password_changed_at is not None
        assert (
            AuthDAO(dbsession).get_pending_verification(email, "password_reset") is None
        )

    @pytest.mark.anyio
    async def test_reset_wrong_code(self, client: AsyncClient, dbsession: Session):
        email = "reset_wrong@example.com"
        await register_and_verify(client, dbsession, email)
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await forgot_password(client, email)

        resp = await verify_code(client, email, "000000", purpose="password_reset")
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_reset_expired_code(self, client: AsyncClient, dbsession: Session):
        email = "reset_expired@example.com"
        await register_and_verify(client, dbsession, email)
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await forgot_password(client, email)

        entry = (
            dbsession.query(EmailVerification)
            .filter(
                EmailVerification.email == email,
                EmailVerification.purpose == "password_reset",
            )
            .first()
        )
        entry.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        entry.code_hash = hash_code("123456")
        dbsession.flush()

        resp = await verify_code(client, email, "123456", purpose="password_reset")
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_reset_max_attempts(self, client: AsyncClient, dbsession: Session):
        email = "reset_max@example.com"
        await register_and_verify(client, dbsession, email)
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await forgot_password(client, email)

        for _ in range(5):
            await verify_code(client, email, "000000", purpose="password_reset")

        entry = (
            dbsession.query(EmailVerification)
            .filter(
                EmailVerification.email == email,
                EmailVerification.purpose == "password_reset",
            )
            .first()
        )
        assert entry.attempts >= 4

        entry.code_hash = hash_code("654321")
        dbsession.flush()
        resp = await verify_code(client, email, "654321", purpose="password_reset")
        assert resp.status_code in (400, 429)

    @pytest.mark.anyio
    async def test_reset_nonexistent_user(self, client: AsyncClient):
        resp = await verify_code(
            client,
            "nobody@example.com",
            "123456",
            purpose="password_reset",
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Change Password
# ═══════════════════════════════════════════════════════════════════════════


class TestChangePassword:
    """POST /auth/change-password (user API key)."""

    @pytest.mark.anyio
    async def test_happy_path(self, client: AsyncClient, dbsession: Session):
        email, old_pw, new_pw = "chpw_ok@example.com", "oldPw@123", "newPw@456"
        _, hdrs = await register_and_verify(client, dbsession, email, password=old_pw)

        resp = await client.post(
            "/v0/auth/change-password",
            json={"current_password": old_pw, "new_password": new_pw},
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert (await authenticate(client, email, old_pw)).status_code == 401
        assert (await authenticate(client, email, new_pw)).status_code == 200

    @pytest.mark.anyio
    async def test_wrong_current_password(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "chpw_wrong@example.com"
        _, hdrs = await register_and_verify(
            client,
            dbsession,
            email,
            password="correctPw@1",
        )

        resp = await client.post(
            "/v0/auth/change-password",
            json={"current_password": "wrongPw@1", "new_password": "newPw@1234"},
            headers=hdrs,
        )
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_sets_password_changed_at(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email, old_pw = "chpw_ts@example.com", "oldPw@123"
        uid, hdrs = await register_and_verify(client, dbsession, email, password=old_pw)

        ea = AuthDAO(dbsession).get_email_credentials(uid)
        assert ea.password_changed_at is None

        await client.post(
            "/v0/auth/change-password",
            json={"current_password": old_pw, "new_password": "newPw@12345"},
            headers=hdrs,
        )

        dbsession.expire_all()
        ea = AuthDAO(dbsession).get_email_credentials(uid)
        assert ea.password_changed_at is not None

    @pytest.mark.anyio
    async def test_new_password_too_short(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email, old_pw = "chpw_short@example.com", "oldPw@123"
        _, hdrs = await register_and_verify(client, dbsession, email, password=old_pw)

        resp = await client.post(
            "/v0/auth/change-password",
            json={"current_password": old_pw, "new_password": "short"},
            headers=hdrs,
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Set Password (OAuth user adds email/password)
# ═══════════════════════════════════════════════════════════════════════════


class TestSetPassword:
    """POST /auth/set-password (user API key)."""

    @pytest.mark.anyio
    async def test_happy_path(self, client: AsyncClient, dbsession: Session):
        _, hdrs = await create_oauth_user(client, dbsession, "setpw_ok@example.com")
        resp = await client.post(
            "/v0/auth/set-password",
            json={"new_password": "newSecureP@ss1"},
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert "Password set successfully" in resp.json()["message"]

    @pytest.mark.anyio
    async def test_too_short(self, client: AsyncClient, dbsession: Session):
        _, hdrs = await create_oauth_user(client, dbsession, "setpw_short@example.com")
        resp = await client.post(
            "/v0/auth/set-password",
            json={"new_password": "short"},
            headers=hdrs,
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_already_has_password(self, client: AsyncClient, dbsession: Session):
        email = "setpw_exists@example.com"
        _, hdrs = await register_and_verify(client, dbsession, email)
        resp = await client.post(
            "/v0/auth/set-password",
            json={"new_password": "anotherP@ss1"},
            headers=hdrs,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "password_already_set"

    @pytest.mark.anyio
    async def test_then_login(self, client: AsyncClient, dbsession: Session):
        email, pw = "setpw_login@example.com", "brandNew@1"
        _, hdrs = await create_oauth_user(client, dbsession, email)
        await client.post(
            "/v0/auth/set-password",
            json={"new_password": pw},
            headers=hdrs,
        )

        resp = await authenticate(client, email, pw)
        assert resp.status_code == 200
        assert resp.json()["email"] == email


# ═══════════════════════════════════════════════════════════════════════════
# Resend Verification
# ═══════════════════════════════════════════════════════════════════════════


class TestResendVerification:
    """POST /admin/auth/resend-verification."""

    @pytest.mark.anyio
    async def test_signup_resend(self, client: AsyncClient, dbsession: Session):
        email = "resend_ok@example.com"
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await register(client, email)

        dao = AuthDAO(dbsession)
        first_hash = dao.get_pending_verification(email, "signup").code_hash

        # Push created_at past the 60-second cooldown
        entry = dao.get_pending_verification(email, "signup")
        entry.created_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        dbsession.flush()

        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            resp = await client.post(
                "/v0/admin/auth/resend-verification",
                json={"email": email, "purpose": "signup"},
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == 200
        assert dao.get_pending_verification(email, "signup").code_hash != first_hash

    @pytest.mark.anyio
    async def test_no_pending(self, client: AsyncClient):
        resp = await client.post(
            "/v0/admin/auth/resend-verification",
            json={"email": "nopending@example.com", "purpose": "signup"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_cooldown(self, client: AsyncClient, dbsession: Session):
        email = "resend_cd@example.com"
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            await register(client, email)

        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            resp = await client.post(
                "/v0/admin/auth/resend-verification",
                json={"email": email, "purpose": "signup"},
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "cooldown"
        assert "retry_after" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════════
# Providers For Email
# ═══════════════════════════════════════════════════════════════════════════


class TestProvidersForEmail:
    """GET /admin/auth/providers-for-email."""

    @pytest.mark.anyio
    async def test_with_email_account(self, client: AsyncClient, dbsession: Session):
        email = "prov_email@example.com"
        await register_and_verify(client, dbsession, email)
        resp = await client.get(
            f"/v0/admin/auth/providers-for-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert "email" in resp.json()["providers"]

    @pytest.mark.anyio
    async def test_with_oauth(self, client: AsyncClient, dbsession: Session):
        email = "prov_oauth@example.com"
        await create_oauth_user(client, dbsession, email, provider="github")
        resp = await client.get(
            f"/v0/admin/auth/providers-for-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert "github" in resp.json()["providers"]

    @pytest.mark.anyio
    async def test_nonexistent(self, client: AsyncClient):
        resp = await client.get(
            "/v0/admin/auth/providers-for-email?email=nobody@example.com",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["providers"] == []


# ═══════════════════════════════════════════════════════════════════════════
# Email Credentials
# ═══════════════════════════════════════════════════════════════════════════


class TestEmailCredentials:
    """GET /admin/auth/email-credentials."""

    @pytest.mark.anyio
    async def test_with_account(self, client: AsyncClient, dbsession: Session):
        email = "cred_email@example.com"
        uid, _ = await register_and_verify(client, dbsession, email)
        resp = await client.get(
            f"/v0/admin/auth/email-credentials?user_id={uid}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["has_email_account"] is True
        assert resp.json()["email_verified"] is True

    @pytest.mark.anyio
    async def test_oauth_only(self, client: AsyncClient, dbsession: Session):
        uid, _ = await create_oauth_user(client, dbsession, "cred_oauth@example.com")
        resp = await client.get(
            f"/v0/admin/auth/email-credentials?user_id={uid}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["has_email_account"] is False

    @pytest.mark.anyio
    async def test_shows_password_changed_at(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email, old_pw = "cred_changed@example.com", "oldPw@1234"
        uid, hdrs = await register_and_verify(client, dbsession, email, password=old_pw)

        resp = await client.get(
            f"/v0/admin/auth/email-credentials?user_id={uid}",
            headers=ADMIN_HEADERS,
        )
        assert resp.json()["password_changed_at"] is None

        await client.post(
            "/v0/auth/change-password",
            json={"current_password": old_pw, "new_password": "newPw@12345"},
            headers=hdrs,
        )

        resp = await client.get(
            f"/v0/admin/auth/email-credentials?user_id={uid}",
            headers=ADMIN_HEADERS,
        )
        assert resp.json()["password_changed_at"] is not None


# ═══════════════════════════════════════════════════════════════════════════
# OAuth Account Linking
# ═══════════════════════════════════════════════════════════════════════════


class TestOAuthAccountLinking:
    """POST /admin/auth/account, DELETE /admin/auth/account."""

    @pytest.mark.anyio
    async def test_link_account(self, client: AsyncClient, dbsession: Session):
        email = "link_acct@example.com"
        resp = await client.post(
            "/v0/admin/user",
            json={"email": email},
            headers=ADMIN_HEADERS,
        )
        user_id = resp.json()["id"]

        resp = await client.post(
            "/v0/admin/auth/account",
            json={
                "provider": "google",
                "type": "oauth",
                "provider_account_id": "g-link-test",
                "access_token": "tok",
                "expires_at": 9999999999,
                "scope": "openid",
                "token_type": "Bearer",
                "id_token": "idt",
                "user_id": user_id,
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        # Verify the account was linked
        prov_resp = await client.get(
            f"/v0/admin/auth/providers-for-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert "google" in prov_resp.json()["providers"]

    @pytest.mark.anyio
    async def test_unlink_account(self, client: AsyncClient, dbsession: Session):
        """Link a GitHub account to a real user, then unlink it successfully."""
        email = "unlink_test@example.com"
        uid, hdrs = await register_and_verify(client, dbsession, email)

        # Link a GitHub provider
        link_payload = {
            "provider": "github",
            "type": "oauth",
            "provider_account_id": f"gh-{uid}",
            "access_token": "tok",
            "expires_at": 9999999999,
            "scope": "openid",
            "token_type": "Bearer",
            "id_token": "idt",
            "user_id": uid,
        }
        link_resp = await client.post(
            "/v0/admin/auth/account",
            json=link_payload,
            headers=ADMIN_HEADERS,
        )
        assert link_resp.status_code == 200

        # Confirm github is now a linked provider
        prov_resp = await client.get(
            f"/v0/admin/auth/providers-for-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert "github" in prov_resp.json()["providers"]

        # Unlink
        resp = await client.request(
            "DELETE",
            "/v0/admin/auth/account",
            json=link_payload,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        # Confirm github is no longer linked
        prov_resp = await client.get(
            f"/v0/admin/auth/providers-for-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert "github" not in prov_resp.json()["providers"]


# ═══════════════════════════════════════════════════════════════════════════
# CAPTCHA (Turnstile)
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptcha:
    """CAPTCHA integration on registration and forgot-password."""

    # -- Registration CAPTCHA --

    @pytest.mark.anyio
    async def test_register_skips_when_not_configured(self, client: AsyncClient):
        """Registration succeeds without a token when TURNSTILE_SECRET_KEY is unset."""
        with patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as m:
            m.return_value = True
            resp = await register(client, "turnstile_skip@example.com")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_register_rejects_missing_token(self, client: AsyncClient):
        with patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv:
            mv.return_value = False
            resp = await register(client, "captcha_missing@example.com")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "captcha_failed"

    @pytest.mark.anyio
    async def test_register_rejects_invalid_token(self, client: AsyncClient):
        with patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv:
            mv.return_value = False
            resp = await register(
                client,
                "captcha_bad@example.com",
                captcha_token="bad-token",
            )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "captcha_failed"
        mv.assert_called_once()
        assert mv.call_args[0][0] == "bad-token"

    @pytest.mark.anyio
    async def test_register_succeeds_with_valid_token(self, client: AsyncClient):
        with (
            patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv,
            patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as me,
        ):
            mv.return_value = True
            me.return_value = True
            resp = await register(
                client,
                "captcha_ok@example.com",
                captcha_token="valid-token",
            )
        assert resp.status_code == 200
        mv.assert_called_once()
        assert mv.call_args[0][0] == "valid-token"

    @pytest.mark.anyio
    async def test_register_failure_prevents_side_effects(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "captcha_nofx@example.com"
        with patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv:
            mv.return_value = False
            await register(client, email, captcha_token="bad")
        assert AuthDAO(dbsession).get_pending_verification(email, "signup") is None

    # -- Forgot-password CAPTCHA --

    @pytest.mark.anyio
    async def test_forgot_rejects_missing_token(self, client: AsyncClient):
        with patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv:
            mv.return_value = False
            resp = await forgot_password(client, "captcha_forgot@example.com")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "captcha_failed"

    @pytest.mark.anyio
    async def test_forgot_rejects_invalid_token(self, client: AsyncClient):
        with patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv:
            mv.return_value = False
            resp = await forgot_password(
                client,
                "captcha_forgot_bad@example.com",
                captcha_token="bad-token",
            )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "captcha_failed"
        assert mv.call_args[0][0] == "bad-token"

    @pytest.mark.anyio
    async def test_forgot_succeeds_with_valid_token(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "captcha_forgot_ok@example.com"
        await register_and_verify(client, dbsession, email)
        with (
            patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv,
            patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as me,
        ):
            mv.return_value = True
            me.return_value = True
            resp = await forgot_password(client, email, captcha_token="valid-token")
        assert resp.status_code == 200
        assert mv.call_args[0][0] == "valid-token"

    @pytest.mark.anyio
    async def test_forgot_failure_prevents_side_effects(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email = "captcha_forgot_nofx@example.com"
        await register_and_verify(client, dbsession, email)
        with patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mv:
            mv.return_value = False
            await forgot_password(client, email, captcha_token="bad")
        assert (
            AuthDAO(dbsession).get_pending_verification(email, "password_reset") is None
        )


# ═══════════════════════════════════════════════════════════════════════════
# MFA Setup
# ═══════════════════════════════════════════════════════════════════════════


class TestMFASetup:
    """POST /auth/mfa/setup."""

    @pytest.mark.anyio
    async def test_returns_qr_uri(self, client: AsyncClient, dbsession: Session):
        _, hdrs = await register_and_verify(client, dbsession, "mfa_setup@example.com")
        resp = await client.post("/v0/auth/mfa/setup", headers=hdrs)
        assert resp.status_code == 200
        uri = resp.json()["qr_code_uri"]
        assert uri.startswith("otpauth://totp/")
        assert "Unify" in uri

    @pytest.mark.anyio
    async def test_already_enabled(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(client, dbsession, "mfa_dup@example.com")
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        resp = await client.post("/v0/auth/mfa/setup", headers=hdrs)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "mfa_already_enabled"


# ═══════════════════════════════════════════════════════════════════════════
# MFA Confirm
# ═══════════════════════════════════════════════════════════════════════════


class TestMFAConfirm:
    """POST /auth/mfa/confirm."""

    @pytest.mark.anyio
    async def test_happy_path(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(client, dbsession, "mfa_conf@example.com")
        _, codes = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        assert len(codes) == 10

        dbsession.expire_all()
        cred = AuthDAO(dbsession).get_enabled_totp(uid)
        assert cred is not None
        assert cred.enabled is True
        assert cred.confirmed_at is not None

    @pytest.mark.anyio
    async def test_wrong_code(self, client: AsyncClient, dbsession: Session):
        _, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_conf_bad@example.com",
        )
        await client.post("/v0/auth/mfa/setup", headers=hdrs)
        resp = await client.post(
            "/v0/auth/mfa/confirm",
            json={"code": "000000"},
            headers=hdrs,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_code"

    @pytest.mark.anyio
    async def test_no_pending_setup(self, client: AsyncClient, dbsession: Session):
        _, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_conf_nop@example.com",
        )
        resp = await client.post(
            "/v0/auth/mfa/confirm",
            json={"code": "123456"},
            headers=hdrs,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "no_pending_setup"


# ═══════════════════════════════════════════════════════════════════════════
# MFA Status
# ═══════════════════════════════════════════════════════════════════════════


class TestMFAStatus:
    """GET /auth/mfa/status (user API key)."""

    @pytest.mark.anyio
    async def test_not_enabled(self, client: AsyncClient, dbsession: Session):
        _, hdrs = await register_and_verify(client, dbsession, "mfa_st_off@example.com")
        resp = await client.get("/v0/auth/mfa/status", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["method"] is None
        assert data["recovery_codes_remaining"] == 0

    @pytest.mark.anyio
    async def test_enabled(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_st_on@example.com",
        )
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        resp = await client.get("/v0/auth/mfa/status", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["method"] == "totp"
        assert data["confirmed_at"] is not None
        assert data["recovery_codes_remaining"] == 10


# ═══════════════════════════════════════════════════════════════════════════
# MFA Disable
# ═══════════════════════════════════════════════════════════════════════════


class TestMFADisable:
    """DELETE /auth/mfa."""

    @pytest.mark.anyio
    async def test_with_totp(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_dis_t@example.com",
        )
        secret, _ = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        advance_totp_window(dbsession, uid)

        resp = await client.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"code": pyotp.TOTP(secret).now()},
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert "disabled" in resp.json()["message"]

        dbsession.expire_all()
        assert AuthDAO(dbsession).has_enabled_mfa(uid) is False

    @pytest.mark.anyio
    async def test_wrong_totp(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_dis_bad@example.com",
        )
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        resp = await client.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"code": "000000"},
            headers=hdrs,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_code"
        assert AuthDAO(dbsession).has_enabled_mfa(uid) is True

    @pytest.mark.anyio
    async def test_with_recovery_code(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_dis_rec@example.com",
        )
        _, codes = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        resp = await client.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"recovery_code": codes[0]},
            headers=hdrs,
        )
        assert resp.status_code == 200

        dbsession.expire_all()
        assert AuthDAO(dbsession).has_enabled_mfa(uid) is False

    @pytest.mark.anyio
    async def test_blocked_by_org(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_dis_org@example.com",
        )
        secret, _ = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        from orchestra.db.dao.organization_dao import OrganizationDAO

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "Secure Org MFA Test"},
            headers=hdrs,
        )
        assert org_resp.status_code == 201
        OrganizationDAO(dbsession).update_mfa_settings(
            org_id=org_resp.json()["id"],
            require_mfa=True,
        )
        dbsession.flush()

        advance_totp_window(dbsession, uid)

        resp = await client.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"code": pyotp.TOTP(secret).now()},
            headers=hdrs,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "mfa_required_by_org"

    @pytest.mark.anyio
    async def test_not_enabled(self, client: AsyncClient, dbsession: Session):
        _, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_dis_none@example.com",
        )
        resp = await client.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"code": "123456"},
            headers=hdrs,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "mfa_not_enabled"

    @pytest.mark.anyio
    async def test_after_disable_login_no_mfa(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        email, pw = "mfa_dis_login@example.com", "secureP@ss1"
        uid, hdrs = await register_and_verify(client, dbsession, email, password=pw)
        _, codes = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        await client.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"recovery_code": codes[0]},
            headers=hdrs,
        )

        resp = await authenticate(client, email, pw)
        assert resp.status_code == 200
        assert resp.json()["mfa_required"] is False


# ═══════════════════════════════════════════════════════════════════════════
# MFA Recovery Codes
# ═══════════════════════════════════════════════════════════════════════════


class TestMFARecoveryCodes:
    """POST /auth/mfa/recovery-codes (requires TOTP code in body)."""

    @pytest.mark.anyio
    async def test_regenerate(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_regen@example.com",
        )
        secret, old_codes = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        advance_totp_window(dbsession, uid)

        totp = pyotp.TOTP(secret)
        resp = await client.post(
            "/v0/auth/mfa/recovery-codes",
            json={"code": totp.now()},
            headers=hdrs,
        )
        assert resp.status_code == 200
        new_codes = resp.json()["recovery_codes"]
        assert len(new_codes) == 10
        assert set(new_codes) != set(old_codes)

    @pytest.mark.anyio
    async def test_old_codes_rejected_after_regen(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        uid, hdrs = await register_and_verify(client, dbsession, "mfa_old@example.com")
        secret, old_codes = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        advance_totp_window(dbsession, uid)

        totp = pyotp.TOTP(secret)
        await client.post(
            "/v0/auth/mfa/recovery-codes",
            json={"code": totp.now()},
            headers=hdrs,
        )

        resp = await client.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": old_codes[0]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_recovery_code"

    @pytest.mark.anyio
    async def test_new_codes_accepted_after_regen(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        uid, hdrs = await register_and_verify(client, dbsession, "mfa_new@example.com")
        secret, _ = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        advance_totp_window(dbsession, uid)

        totp = pyotp.TOTP(secret)
        resp = await client.post(
            "/v0/auth/mfa/recovery-codes",
            json={"code": totp.now()},
            headers=hdrs,
        )
        assert resp.status_code == 200
        new_codes = resp.json()["recovery_codes"]

        resp = await client.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": new_codes[0]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["remaining_codes"] == 9

    @pytest.mark.anyio
    async def test_regenerate_when_not_enabled(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        _, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_regen_none@example.com",
        )
        resp = await client.post(
            "/v0/auth/mfa/recovery-codes",
            json={"code": "000000"},
            headers=hdrs,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "mfa_not_enabled"


# ═══════════════════════════════════════════════════════════════════════════
# MFA Login Verify (admin endpoints)
# ═══════════════════════════════════════════════════════════════════════════


class TestMFALoginVerify:
    """POST /admin/auth/mfa/verify, /admin/auth/mfa/verify-recovery."""

    @pytest.mark.anyio
    async def test_correct_totp(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_lv_ok@example.com",
        )
        secret, _ = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)
        advance_totp_window(dbsession, uid)

        resp = await client.post(
            "/v0/admin/auth/mfa/verify",
            json={"user_id": uid, "code": pyotp.TOTP(secret).now()},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    @pytest.mark.anyio
    async def test_wrong_totp(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_lv_bad@example.com",
        )
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        resp = await client.post(
            "/v0/admin/auth/mfa/verify",
            json={"user_id": uid, "code": "000000"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_code"

    @pytest.mark.anyio
    async def test_no_mfa(self, client: AsyncClient, dbsession: Session):
        uid, _ = await register_and_verify(client, dbsession, "mfa_lv_none@example.com")
        resp = await client.post(
            "/v0/admin/auth/mfa/verify",
            json={"user_id": uid, "code": "123456"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "mfa_not_enabled"

    @pytest.mark.anyio
    async def test_recovery_login_ok(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_lv_rec@example.com",
        )
        _, codes = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        resp = await client.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": codes[0]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["remaining_codes"] == 9

    @pytest.mark.anyio
    async def test_recovery_reuse_rejected(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_lv_reuse@example.com",
        )
        _, codes = await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        await client.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": codes[0]},
            headers=ADMIN_HEADERS,
        )
        resp = await client.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": codes[0]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_recovery_code"

    @pytest.mark.anyio
    async def test_recovery_wrong_code(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "mfa_lv_recbad@example.com",
        )
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        resp = await client.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": "wrongcode"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_recovery_code"

    @pytest.mark.anyio
    async def test_recovery_no_mfa(self, client: AsyncClient, dbsession: Session):
        uid, _ = await register_and_verify(
            client,
            dbsession,
            "mfa_lv_recnone@example.com",
        )
        resp = await client.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": "anycode1"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "mfa_not_enabled"


# ═══════════════════════════════════════════════════════════════════════════
# MFA Status By Email (admin)
# ═══════════════════════════════════════════════════════════════════════════


class TestMFAStatusByEmail:
    """GET /admin/auth/mfa/status-by-email."""

    @pytest.mark.anyio
    async def test_has_mfa(self, client: AsyncClient, dbsession: Session):
        email = "mfa_sbe_on@example.com"
        uid, hdrs = await register_and_verify(client, dbsession, email)
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        resp = await client.get(
            f"/v0/admin/auth/mfa/status-by-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["user_found"] is True
        assert resp.json()["mfa_enabled"] is True

    @pytest.mark.anyio
    async def test_no_mfa(self, client: AsyncClient, dbsession: Session):
        email = "mfa_sbe_off@example.com"
        await register_and_verify(client, dbsession, email)

        resp = await client.get(
            f"/v0/admin/auth/mfa/status-by-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["user_found"] is True
        assert resp.json()["mfa_enabled"] is False

    @pytest.mark.anyio
    async def test_nonexistent(self, client: AsyncClient):
        resp = await client.get(
            "/v0/admin/auth/mfa/status-by-email?email=ghost@example.com",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["user_found"] is False
        assert resp.json()["mfa_enabled"] is False


# ═══════════════════════════════════════════════════════════════════════════
# MFA Enforcement Status (admin)
# ═══════════════════════════════════════════════════════════════════════════


class TestMFAEnforcementStatus:
    """GET /admin/auth/mfa/enforcement-status."""

    @pytest.mark.anyio
    async def test_enforced_no_mfa_setup_required(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        uid, hdrs = await register_and_verify(
            client,
            dbsession,
            "enf_nomfa@example.com",
        )

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "EnfOrg"},
            headers=hdrs,
        )
        org_id = org_resp.json()["id"]

        from orchestra.db.dao.organization_dao import OrganizationDAO

        OrganizationDAO(dbsession).update_mfa_settings(org_id=org_id, require_mfa=True)
        dbsession.flush()

        resp = await client.get(
            f"/v0/admin/auth/mfa/enforcement-status?user_id={uid}&org_id={org_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enforced"] is True
        assert data["has_mfa"] is False
        assert data["setup_required"] is True

    @pytest.mark.anyio
    async def test_enforced_has_mfa_not_required(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        uid, hdrs = await register_and_verify(client, dbsession, "enf_mfa@example.com")
        await setup_and_confirm_mfa(client, dbsession, uid, hdrs)

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "EnfOrg2"},
            headers=hdrs,
        )
        org_id = org_resp.json()["id"]

        from orchestra.db.dao.organization_dao import OrganizationDAO

        OrganizationDAO(dbsession).update_mfa_settings(org_id=org_id, require_mfa=True)
        dbsession.flush()

        resp = await client.get(
            f"/v0/admin/auth/mfa/enforcement-status?user_id={uid}&org_id={org_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enforced"] is True
        assert data["has_mfa"] is True
        assert data["setup_required"] is False

    @pytest.mark.anyio
    async def test_not_enforced(self, client: AsyncClient, dbsession: Session):
        uid, hdrs = await register_and_verify(client, dbsession, "enf_off@example.com")

        org_resp = await client.post(
            "/v0/organizations",
            json={"name": "RelaxOrg"},
            headers=hdrs,
        )
        org_id = org_resp.json()["id"]

        resp = await client.get(
            f"/v0/admin/auth/mfa/enforcement-status?user_id={uid}&org_id={org_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enforced"] is False
        assert data["setup_required"] is False

    @pytest.mark.anyio
    async def test_org_not_found(self, client: AsyncClient, dbsession: Session):
        uid, _ = await register_and_verify(client, dbsession, "enf_404@example.com")
        resp = await client.get(
            f"/v0/admin/auth/mfa/enforcement-status?user_id={uid}&org_id=99999",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404
