"""
True end-to-end auth flow tests against a running Orchestra instance.

Each test exercises a complete, realistic user journey over real HTTP.
Unlike the API tests in ``test_auth_api`` (which use an in-process ASGI
test client), these tests hit a **running** Orchestra server — exercising
the full deployment stack including ASGI server, middleware, and the
network layer.

Only DB access is used for test setup (setting known verification codes,
reading TOTP secrets) — never for assertions on the system's behaviour.

Prerequisites:
  - Orchestra running locally    (poetry run python -m orchestra)
  - PostgreSQL accessible        (localhost:5432 or ORCHESTRA_DB_URL)

Skip: All tests skip when Orchestra is not reachable.

Sections:
  - SignupFlows: register → verify → login (happy path, password reset, multi-user)
  - ChangePasswordFlows: change password → re-login
  - ResendVerificationFlows: resend code → verify with new code
  - MFAFlows: setup → login-with-MFA, disable, recovery regen
  - MFAEnforcementFlows: org requires MFA → can't disable
  - OAuthFlows: set password, multi-provider linking, MFA on OAuth user
"""

import httpx
import pyotp
import pytest

from orchestra.db.dao.auth_dao import hash_code

from .conftest import (
    ADMIN_HEADERS,
    E2E_BASE_URL,
    e2e_advance_totp_window,
    e2e_authenticate,
    e2e_create_oauth_user,
    e2e_db_execute,
    e2e_forgot_password,
    e2e_register,
    e2e_register_and_verify,
    e2e_server_reachable,
    e2e_setup_and_confirm_mfa,
    e2e_verify,
    e2e_verify_code,
    unique_email,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Skip the entire module when the server is not running
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _require_server():
    if not e2e_server_reachable():
        pytest.skip(f"Orchestra server at {E2E_BASE_URL} is not reachable")


@pytest.fixture
async def e2e():
    """Async HTTP client connected to the running Orchestra server."""
    async with httpx.AsyncClient(base_url=E2E_BASE_URL, timeout=30) as client:
        yield client


# ═══════════════════════════════════════════════════════════════════════════
# Signup Flows
# ═══════════════════════════════════════════════════════════════════════════


class TestSignupFlows:
    """Full register → verify → authenticate journeys."""

    async def test_register_verify_login(self, e2e: httpx.AsyncClient):
        """Register → verify email → login with password."""
        email, pw = unique_email("signup"), "e2ePw@123"
        await e2e_register_and_verify(e2e, email, password=pw)

        resp = await e2e_authenticate(e2e, email, pw)
        assert resp.status_code == 200
        assert resp.json()["email"] == email
        assert resp.json()["mfa_required"] is False

    async def test_register_verify_forgot_reset_login(self, e2e: httpx.AsyncClient):
        """Register → verify → forgot password → reset → login with new password."""
        email = unique_email("reset")
        old_pw, new_pw = "originalPw@1", "brandNewPw@2"
        await e2e_register_and_verify(e2e, email, password=old_pw)

        # Forgot password
        await e2e_forgot_password(e2e, email)

        # Set known code in DB
        code = "111222"
        e2e_db_execute(
            "UPDATE email_verification "
            "SET code_hash = :hash, attempts = 0 "
            "WHERE email = :email AND purpose = 'password_reset'",
            {"hash": hash_code(code), "email": email},
        )

        # Verify + reset
        vr = await e2e_verify_code(e2e, email, code, purpose="password_reset")
        assert vr.status_code == 200
        token = vr.json()["token"]

        resp = await e2e.post(
            "/v0/admin/auth/reset-password",
            json={"token": token, "new_password": new_pw},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        # New password works, old does not
        assert (await e2e_authenticate(e2e, email, new_pw)).status_code == 200
        assert (await e2e_authenticate(e2e, email, old_pw)).status_code == 401

    async def test_multiple_users_independent(self, e2e: httpx.AsyncClient):
        """Several users register and authenticate without interference."""
        users = [
            (unique_email("multi_a"), "pwA@12345"),
            (unique_email("multi_b"), "pwB@12345"),
            (unique_email("multi_c"), "pwC@12345"),
        ]
        for email, pw in users:
            await e2e_register_and_verify(e2e, email, password=pw)

        for email, pw in users:
            assert (await e2e_authenticate(e2e, email, pw)).status_code == 200

        # Cross-user password must not work
        assert (
            await e2e_authenticate(e2e, users[0][0], users[1][1])
        ).status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Change Password Flows
# ═══════════════════════════════════════════════════════════════════════════


class TestChangePasswordFlows:
    """Register → verify → change password → re-login journeys."""

    async def test_change_password_then_relogin(self, e2e: httpx.AsyncClient):
        """
        1. Register + verify
        2. Change password (old → new)
        3. Login with new password succeeds
        4. Login with old password fails
        """
        email = unique_email("chpw")
        old_pw, new_pw = "oldPw@1234", "newPw@5678"
        _, hdrs = await e2e_register_and_verify(e2e, email, password=old_pw)

        resp = await e2e.post(
            "/v0/auth/change-password",
            json={"current_password": old_pw, "new_password": new_pw},
            headers=hdrs,
        )
        assert resp.status_code == 200

        assert (await e2e_authenticate(e2e, email, new_pw)).status_code == 200
        assert (await e2e_authenticate(e2e, email, old_pw)).status_code == 401

    async def test_change_password_twice(self, e2e: httpx.AsyncClient):
        """Change password twice in succession — only the latest password works."""
        email = unique_email("chpw2")
        pw1, pw2, pw3 = "first@Pass1", "second@Pass2", "third@Pass3"
        _, hdrs = await e2e_register_and_verify(e2e, email, password=pw1)

        resp = await e2e.post(
            "/v0/auth/change-password",
            json={"current_password": pw1, "new_password": pw2},
            headers=hdrs,
        )
        assert resp.status_code == 200

        resp = await e2e.post(
            "/v0/auth/change-password",
            json={"current_password": pw2, "new_password": pw3},
            headers=hdrs,
        )
        assert resp.status_code == 200

        assert (await e2e_authenticate(e2e, email, pw3)).status_code == 200
        assert (await e2e_authenticate(e2e, email, pw2)).status_code == 401
        assert (await e2e_authenticate(e2e, email, pw1)).status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Resend Verification Flows
# ═══════════════════════════════════════════════════════════════════════════


class TestResendVerificationFlows:
    """Register → wrong code → resend → verify with new code → login."""

    async def test_resend_then_verify(self, e2e: httpx.AsyncClient):
        """
        1. Register (email sent)
        2. Try wrong code → rejected
        3. Resend verification
        4. Use new code → success
        5. Login works
        """
        email, pw = unique_email("resend"), "resendPw@1"

        resp = await e2e_register(e2e, email, pw)
        assert resp.status_code == 200

        # Wrong code
        vr = await e2e_verify_code(e2e, email, "000000")
        assert vr.status_code == 400

        # Push created_at back so the 60-second cooldown doesn't block resend
        e2e_db_execute(
            "UPDATE email_verification "
            "SET created_at = NOW() - INTERVAL '120 seconds' "
            "WHERE email = :email AND purpose = 'signup'",
            {"email": email},
        )

        # Resend
        resend_resp = await e2e.post(
            "/v0/admin/auth/resend-verification",
            json={"email": email, "purpose": "signup"},
            headers=ADMIN_HEADERS,
        )
        assert resend_resp.status_code == 200

        # Set known code on the new entry
        code = "654321"
        e2e_db_execute(
            "UPDATE email_verification "
            "SET code_hash = :hash, attempts = 0 "
            "WHERE email = :email AND purpose = 'signup'",
            {"hash": hash_code(code), "email": email},
        )

        # Verify with new code → create user
        resp = await e2e_verify(e2e, email, code)
        assert resp.status_code == 200

        # Login
        assert (await e2e_authenticate(e2e, email, pw)).status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# MFA Flows
# ═══════════════════════════════════════════════════════════════════════════


class TestMFAFlows:
    """Register → MFA setup → login-with-MFA journeys."""

    async def test_register_mfa_setup_login_verify(self, e2e: httpx.AsyncClient):
        """
        1. Register + verify
        2. Setup + confirm MFA
        3. Authenticate → mfa_required=True
        4. Verify TOTP via admin endpoint
        5. Verify recovery code also works
        6. Check MFA status
        """
        email, pw = unique_email("mfa"), "e2eMfa@1"
        uid, hdrs = await e2e_register_and_verify(e2e, email, password=pw)
        secret, codes = await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)

        # Authenticate → mfa_required
        resp = await e2e_authenticate(e2e, email, pw)
        assert resp.json()["mfa_required"] is True

        # Verify TOTP
        e2e_advance_totp_window(uid)
        totp = pyotp.TOTP(secret)
        vr = await e2e.post(
            "/v0/admin/auth/mfa/verify",
            json={"user_id": uid, "code": totp.now()},
            headers=ADMIN_HEADERS,
        )
        assert vr.json()["success"] is True

        # Verify recovery code
        rr = await e2e.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": codes[0]},
            headers=ADMIN_HEADERS,
        )
        assert rr.json()["success"] is True
        assert rr.json()["remaining_codes"] == 9

        # MFA status
        sr = await e2e.get("/v0/auth/mfa/status", headers=hdrs)
        assert sr.json()["enabled"] is True
        assert sr.json()["recovery_codes_remaining"] == 9

    async def test_mfa_setup_disable_relogin(self, e2e: httpx.AsyncClient):
        """
        1. Register + verify
        2. Enable MFA
        3. Authenticate → mfa_required=True
        4. Disable MFA with recovery code
        5. Authenticate → mfa_required=False
        6. MFA status shows disabled
        """
        email, pw = unique_email("mfa_toggle"), "togglePw@1"
        uid, hdrs = await e2e_register_and_verify(e2e, email, password=pw)
        _, codes = await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)

        assert (await e2e_authenticate(e2e, email, pw)).json()["mfa_required"] is True

        # Disable MFA
        resp = await e2e.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"recovery_code": codes[0]},
            headers=hdrs,
        )
        assert resp.status_code == 200

        assert (await e2e_authenticate(e2e, email, pw)).json()["mfa_required"] is False
        assert (await e2e.get("/v0/auth/mfa/status", headers=hdrs)).json()[
            "enabled"
        ] is False

    async def test_recovery_code_regen_invalidates_old(self, e2e: httpx.AsyncClient):
        """
        1. Register + verify + MFA setup
        2. Regenerate recovery codes (with TOTP)
        3. Old codes fail verification
        4. New codes succeed
        """
        email, pw = unique_email("regen"), "regenPw@1"
        uid, hdrs = await e2e_register_and_verify(e2e, email, password=pw)
        secret, old_codes = await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)
        e2e_advance_totp_window(uid)

        # Regenerate
        totp = pyotp.TOTP(secret)
        resp = await e2e.post(
            "/v0/auth/mfa/recovery-codes",
            json={"code": totp.now()},
            headers=hdrs,
        )
        assert resp.status_code == 200
        new_codes = resp.json()["recovery_codes"]
        assert len(new_codes) == 10
        assert set(new_codes) != set(old_codes)

        # Old code rejected
        resp = await e2e.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": old_codes[0]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

        # New code accepted
        resp = await e2e.post(
            "/v0/admin/auth/mfa/verify-recovery",
            json={"user_id": uid, "code": new_codes[0]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["remaining_codes"] == 9

    async def test_mfa_disable_then_reenable(self, e2e: httpx.AsyncClient):
        """
        1. Register + verify + MFA setup
        2. Disable MFA → mfa_required=False
        3. Re-enable MFA → mfa_required=True again
        """
        email, pw = unique_email("mfa_reenable"), "reEnablePw@1"
        uid, hdrs = await e2e_register_and_verify(e2e, email, password=pw)
        _, codes = await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)

        # Disable
        resp = await e2e.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"recovery_code": codes[0]},
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert (await e2e_authenticate(e2e, email, pw)).json()["mfa_required"] is False

        # Re-enable
        secret2, _codes2 = await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)
        assert (await e2e_authenticate(e2e, email, pw)).json()["mfa_required"] is True

        # Verify the new TOTP works
        e2e_advance_totp_window(uid)
        totp = pyotp.TOTP(secret2)
        vr = await e2e.post(
            "/v0/admin/auth/mfa/verify",
            json={"user_id": uid, "code": totp.now()},
            headers=ADMIN_HEADERS,
        )
        assert vr.json()["success"] is True


# ═══════════════════════════════════════════════════════════════════════════
# MFA Enforcement Flows (org requires MFA)
# ═══════════════════════════════════════════════════════════════════════════


class TestMFAEnforcementFlows:
    """Org requires MFA → user must comply, cannot disable."""

    async def test_org_requires_mfa_cannot_disable(self, e2e: httpx.AsyncClient):
        """
        1. Register + verify
        2. Create org → owner
        3. Enable org MFA requirement
        4. Setup MFA on user
        5. Try to disable MFA → blocked (org requires it)
        6. Enforcement status shows enforced + setup not required
        """
        email, pw = unique_email("enf"), "enforcePw@1"
        uid, hdrs = await e2e_register_and_verify(e2e, email, password=pw)

        # Create org
        resp = await e2e.post(
            "/v0/organizations",
            json={"name": f"EnforceOrg-{uid[:8]}"},
            headers=hdrs,
        )
        assert resp.status_code == 201
        org_id = resp.json()["id"]

        # Enable MFA requirement on org
        resp = await e2e.put(
            f"/v0/organizations/{org_id}/mfa-settings",
            json={"require_mfa": True},
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert resp.json()["require_mfa"] is True

        # Enforcement status before MFA setup → enforced + setup_required
        resp = await e2e.get(
            f"/v0/admin/auth/mfa/enforcement-status?user_id={uid}&org_id={org_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["enforced"] is True
        assert resp.json()["setup_required"] is True

        # Setup MFA
        _, codes = await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)

        # Enforcement status after MFA setup → enforced but not setup_required
        resp = await e2e.get(
            f"/v0/admin/auth/mfa/enforcement-status?user_id={uid}&org_id={org_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["enforced"] is True
        assert resp.json()["setup_required"] is False

        # Try to disable MFA → blocked by org
        resp = await e2e.request(
            "DELETE",
            "/v0/auth/mfa",
            json={"recovery_code": codes[0]},
            headers=hdrs,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "mfa_required_by_org"


# ═══════════════════════════════════════════════════════════════════════════
# OAuth Flows
# ═══════════════════════════════════════════════════════════════════════════


class TestOAuthFlows:
    """OAuth-only user → set password → authenticate with email/password."""

    async def test_oauth_set_password_login(self, e2e: httpx.AsyncClient):
        """OAuth user sets a password, then logs in with email/password."""
        email, pw = unique_email("oauth"), "oauthPw@123"
        _, hdrs = await e2e_create_oauth_user(e2e, email)

        resp = await e2e.post(
            "/v0/auth/set-password",
            json={"new_password": pw},
            headers=hdrs,
        )
        assert resp.status_code == 200

        resp = await e2e_authenticate(e2e, email, pw)
        assert resp.status_code == 200
        assert resp.json()["email"] == email

    async def test_oauth_link_second_provider(self, e2e: httpx.AsyncClient):
        """
        1. Create OAuth user with Google
        2. Link GitHub as second provider
        3. Providers-for-email shows both
        """
        email = unique_email("multi_oauth")
        uid, _ = await e2e_create_oauth_user(e2e, email, provider="google")

        # Link GitHub
        resp = await e2e.post(
            "/v0/admin/auth/account",
            json={
                "provider": "github",
                "type": "oauth",
                "provider_account_id": f"github-{email}",
                "access_token": "tok2",
                "expires_at": 9999999999,
                "scope": "openid",
                "token_type": "Bearer",
                "id_token": "idt2",
                "user_id": uid,
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200

        # Both providers visible
        resp = await e2e.get(
            f"/v0/admin/auth/providers-for-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        providers = resp.json()["providers"]
        assert "google" in providers
        assert "github" in providers

    async def test_oauth_user_enables_mfa(self, e2e: httpx.AsyncClient):
        """
        1. Create OAuth user (Google)
        2. Set password
        3. Enable MFA
        4. Authenticate → mfa_required=True
        5. Verify TOTP
        """
        email, pw = unique_email("oauth_mfa"), "oaMfa@1234"
        uid, hdrs = await e2e_create_oauth_user(e2e, email)

        # Set password first
        resp = await e2e.post(
            "/v0/auth/set-password",
            json={"new_password": pw},
            headers=hdrs,
        )
        assert resp.status_code == 200

        # Enable MFA
        secret, _ = await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)

        # Login → MFA required
        resp = await e2e_authenticate(e2e, email, pw)
        assert resp.json()["mfa_required"] is True

        # Verify TOTP
        e2e_advance_totp_window(uid)
        totp = pyotp.TOTP(secret)
        vr = await e2e.post(
            "/v0/admin/auth/mfa/verify",
            json={"user_id": uid, "code": totp.now()},
            headers=ADMIN_HEADERS,
        )
        assert vr.json()["success"] is True

    async def test_oauth_mfa_status_by_email(self, e2e: httpx.AsyncClient):
        """
        1. Create OAuth user
        2. Check MFA status by email → not enabled
        3. Enable MFA
        4. Check MFA status by email → enabled
        """
        email = unique_email("oauth_mfa_st")
        uid, hdrs = await e2e_create_oauth_user(e2e, email)

        # Not enabled
        resp = await e2e.get(
            f"/v0/admin/auth/mfa/status-by-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["mfa_enabled"] is False

        # Enable MFA (need password first)
        await e2e.post(
            "/v0/auth/set-password",
            json={"new_password": "tempPw@1234"},
            headers=hdrs,
        )
        await e2e_setup_and_confirm_mfa(e2e, uid, hdrs)

        # Now enabled
        resp = await e2e.get(
            f"/v0/admin/auth/mfa/status-by-email?email={email}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["mfa_enabled"] is True
