"""
HTTP integration tests for all MFA API endpoints.

Covers the MFA endpoints that were previously only tested at the DAO
unit-test level (test_mfa.py).  These tests hit the real FastAPI app
with a real database, exercising the full request → view → DAO → DB
path.

Manual test cases covered:
  8.1  — MFA setup returns QR provisioning URI
  8.3  — Confirm TOTP with correct code → recovery codes returned
  8.4  — Confirm with wrong code → error
  8.6  — After confirm, MFACredential.enabled=True and confirmed_at set
  9.1  — Login MFA verify with correct TOTP → success
  9.2  — Login MFA verify with wrong TOTP → error
  9.6  — Login MFA verify with recovery code → success + remaining
  9.8  — Reuse consumed recovery code → error
  10.1 — Disable MFA with TOTP code
  10.2 — Disable MFA with wrong TOTP → error
  10.3 — Disable MFA with recovery code
  10.4 — Disable MFA blocked by org enforcement
  10.5 — After disabling, login no longer requires MFA
  11.2 — Regenerate recovery codes (old invalidated, new work)
  11.3 — Old recovery codes rejected after regeneration
  11.4 — New recovery codes accepted after regeneration
  Auth — authenticate returns mfa_required=True when MFA enabled
  Status — GET /auth/mfa/status reflects enabled/disabled
  Status-by-email — GET /admin/auth/mfa/status-by-email
  E2E  — Full register → MFA setup → login with MFA → verify flow
"""

import os
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.email_verification_dao import EmailVerificationDAO, hash_code
from orchestra.db.dao.mfa_credential_dao import MFACredentialDAO, decrypt_secret

ADMIN_HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
    "Content-Type": "application/json",
}


# =============================================================================
# Helpers
# =============================================================================


async def _register_and_verify(
    client: AsyncClient,
    dbsession: Session,
    email: str,
    password: str = "secureP@ss1",
):
    """Register + verify a user, returning (user_id, user_api_headers)."""
    from unittest.mock import AsyncMock, patch

    from orchestra.db.dao.api_key_dao import ApiKeyDAO
    from orchestra.db.dao.user_dao import UserDAO

    # Register (mock email sending and Turnstile CAPTCHA)
    with (
        patch(
            "orchestra.web.api.utils.email.send_email_async",
            new_callable=AsyncMock,
        ) as mock_send,
        patch(
            "orchestra.web.api.auth.views.verify_turnstile_token",
            new_callable=AsyncMock,
        ) as mock_captcha,
    ):
        mock_send.return_value = True
        mock_captcha.return_value = True
        resp = await client.post(
            "/v0/admin/auth/register",
            json={
                "email": email,
                "password": password,
                "name": "Test",
                "last_name": "User",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.json()

    # Set known code
    dao = EmailVerificationDAO(dbsession)
    entry = dao.get_pending(email, "signup")
    assert entry is not None
    code = "123456"
    entry.code_hash = hash_code(code)
    dbsession.flush()

    # Verify code
    verify_resp = await client.post(
        "/v0/admin/auth/verify-code",
        json={"email": email, "code": code, "purpose": "signup"},
        headers=ADMIN_HEADERS,
    )
    assert verify_resp.status_code == 200
    token = verify_resp.json()["token"]

    # Create user
    create_resp = await client.post(
        "/v0/admin/auth/create-user",
        json={"token": token},
        headers=ADMIN_HEADERS,
    )
    assert create_resp.status_code == 200
    user_data = create_resp.json()
    user_id = user_data["id"]

    # Get API key
    user_dao = UserDAO(dbsession)
    user = user_dao.filter(email=email)[0][0]
    api_key_dao = ApiKeyDAO(dbsession)
    keys = api_key_dao.filter(user_id=user.id)
    api_key = keys[0][0].key

    user_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    return user_id, user_headers


async def _setup_and_confirm_mfa(
    client: AsyncClient,
    dbsession: Session,
    user_id: str,
    user_headers: dict,
):
    """
    Run MFA setup + confirm for a user.

    Returns (totp_secret, recovery_codes).
    """
    # Step 1: Setup
    resp = await client.post("/v0/auth/mfa/setup", headers=user_headers)
    assert resp.status_code == 200
    qr_uri = resp.json()["qr_code_uri"]
    assert qr_uri.startswith("otpauth://totp/")

    # Extract the TOTP secret from the pending credential in DB
    mfa_dao = MFACredentialDAO(dbsession)
    credential = mfa_dao.get_pending_totp(user_id)
    assert credential is not None
    secret = decrypt_secret(credential.credential_data)

    # Step 2: Confirm with a valid TOTP code
    totp = pyotp.TOTP(secret)
    code = totp.now()
    resp = await client.post(
        "/v0/auth/mfa/confirm",
        json={"code": code},
        headers=user_headers,
    )
    assert resp.status_code == 200
    recovery_codes = resp.json()["recovery_codes"]
    assert len(recovery_codes) == 10

    return secret, recovery_codes


# =============================================================================
# MFA Setup Tests (8.x)
# =============================================================================


@pytest.mark.anyio
async def test_mfa_setup_returns_qr_uri(client: AsyncClient, dbsession: Session):
    """MFA setup generates a QR provisioning URI (8.1)."""
    email = "mfa_setup@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.post("/v0/auth/mfa/setup", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "qr_code_uri" in data
    assert data["qr_code_uri"].startswith("otpauth://totp/")
    assert "Unify" in data["qr_code_uri"]


@pytest.mark.anyio
async def test_mfa_setup_already_enabled(client: AsyncClient, dbsession: Session):
    """MFA setup fails when MFA is already enabled (idempotency)."""
    email = "mfa_setup_dup@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    # Try to set up again
    resp = await client.post("/v0/auth/mfa/setup", headers=user_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "mfa_already_enabled"


# =============================================================================
# MFA Confirm Tests (8.3, 8.4)
# =============================================================================


@pytest.mark.anyio
async def test_mfa_confirm_happy_path(client: AsyncClient, dbsession: Session):
    """Confirming MFA with correct code enables MFA and returns recovery codes (8.3, 8.6)."""
    email = "mfa_confirm@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    secret, recovery_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    assert len(recovery_codes) == 10

    # Verify credential state in DB
    mfa_dao = MFACredentialDAO(dbsession)
    dbsession.expire_all()
    credential = mfa_dao.get_enabled_totp(user_id)
    assert credential is not None
    assert credential.enabled is True
    assert credential.confirmed_at is not None


@pytest.mark.anyio
async def test_mfa_confirm_wrong_code(client: AsyncClient, dbsession: Session):
    """Confirming MFA with wrong code fails (8.4)."""
    email = "mfa_confirm_bad@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    # Setup first
    await client.post("/v0/auth/mfa/setup", headers=user_headers)

    # Confirm with wrong code
    resp = await client.post(
        "/v0/auth/mfa/confirm",
        json={"code": "000000"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_code"


@pytest.mark.anyio
async def test_mfa_confirm_no_pending_setup(client: AsyncClient, dbsession: Session):
    """Confirming MFA without a pending setup fails."""
    email = "mfa_confirm_nopend@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.post(
        "/v0/auth/mfa/confirm",
        json={"code": "123456"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "no_pending_setup"


# =============================================================================
# MFA Status Tests (8.1)
# =============================================================================


@pytest.mark.anyio
async def test_mfa_status_not_enabled(client: AsyncClient, dbsession: Session):
    """MFA status returns enabled=False when not set up."""
    email = "mfa_status_off@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.get("/v0/auth/mfa/status", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["method"] is None
    assert data["recovery_codes_remaining"] == 0


@pytest.mark.anyio
async def test_mfa_status_enabled(client: AsyncClient, dbsession: Session):
    """MFA status returns enabled=True with recovery code count when enabled."""
    email = "mfa_status_on@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    resp = await client.get("/v0/auth/mfa/status", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["method"] == "totp"
    assert data["confirmed_at"] is not None
    assert data["recovery_codes_remaining"] == 10


# =============================================================================
# MFA Disable Tests (10.1–10.5)
# =============================================================================


@pytest.mark.anyio
async def test_mfa_disable_with_totp(client: AsyncClient, dbsession: Session):
    """Disable MFA with a valid TOTP code (10.1)."""
    email = "mfa_disable_totp@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    secret, _ = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Advance last_used_at to avoid replay protection
    mfa_dao = MFACredentialDAO(dbsession)
    credential = mfa_dao.get_enabled_totp(user_id)
    credential.last_used_at = datetime.now(timezone.utc) - timedelta(seconds=35)
    dbsession.flush()

    totp = pyotp.TOTP(secret)
    resp = await client.request(
        "DELETE",
        "/v0/auth/mfa",
        json={"code": totp.now()},
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert "disabled" in resp.json()["message"]

    # MFA should no longer be enabled
    dbsession.expire_all()
    assert mfa_dao.has_enabled_mfa(user_id) is False


@pytest.mark.anyio
async def test_mfa_disable_wrong_totp(client: AsyncClient, dbsession: Session):
    """Disable MFA with wrong TOTP code fails (10.2)."""
    email = "mfa_disable_bad@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    resp = await client.request(
        "DELETE",
        "/v0/auth/mfa",
        json={"code": "000000"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_code"

    # MFA should still be enabled
    mfa_dao = MFACredentialDAO(dbsession)
    assert mfa_dao.has_enabled_mfa(user_id) is True


@pytest.mark.anyio
async def test_mfa_disable_with_recovery_code(client: AsyncClient, dbsession: Session):
    """Disable MFA with a valid recovery code (10.3)."""
    email = "mfa_disable_rec@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    _, recovery_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    resp = await client.request(
        "DELETE",
        "/v0/auth/mfa",
        json={"recovery_code": recovery_codes[0]},
        headers=user_headers,
    )
    assert resp.status_code == 200
    assert "disabled" in resp.json()["message"]

    # MFA should no longer be enabled
    mfa_dao = MFACredentialDAO(dbsession)
    dbsession.expire_all()
    assert mfa_dao.has_enabled_mfa(user_id) is False


@pytest.mark.anyio
async def test_mfa_disable_blocked_by_org(client: AsyncClient, dbsession: Session):
    """Disable MFA blocked when org requires it (10.4)."""
    email = "mfa_disable_org@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    secret, _ = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Create an org that requires MFA
    from orchestra.db.dao.organization_dao import OrganizationDAO

    org_resp = await client.post(
        "/v0/organizations",
        json={"name": "Secure Org MFA Test"},
        headers=user_headers,
    )
    assert org_resp.status_code == 201
    org_id = org_resp.json()["id"]

    # Enable MFA enforcement on the org
    org_dao = OrganizationDAO(dbsession)
    org_dao.update_mfa_settings(org_id=org_id, require_mfa=True)
    dbsession.flush()

    # Advance last_used_at to avoid replay protection
    mfa_dao = MFACredentialDAO(dbsession)
    credential = mfa_dao.get_enabled_totp(user_id)
    credential.last_used_at = datetime.now(timezone.utc) - timedelta(seconds=35)
    dbsession.flush()

    totp = pyotp.TOTP(secret)
    resp = await client.request(
        "DELETE",
        "/v0/auth/mfa",
        json={"code": totp.now()},
        headers=user_headers,
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "mfa_required_by_org"
    assert "Secure Org MFA Test" in detail["message"]
    assert "Secure Org MFA Test" in detail["org_names"]


@pytest.mark.anyio
async def test_mfa_disable_not_enabled(client: AsyncClient, dbsession: Session):
    """Disable MFA fails when MFA is not enabled."""
    email = "mfa_disable_none@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.request(
        "DELETE",
        "/v0/auth/mfa",
        json={"code": "123456"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "mfa_not_enabled"


@pytest.mark.anyio
async def test_after_mfa_disable_login_no_mfa(client: AsyncClient, dbsession: Session):
    """After disabling MFA, login no longer requires TOTP (10.5)."""
    email = "mfa_disable_login@example.com"
    password = "secureP@ss1"
    user_id, user_headers = await _register_and_verify(
        client,
        dbsession,
        email,
        password=password,
    )
    secret, recovery_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Disable MFA using recovery code
    resp = await client.request(
        "DELETE",
        "/v0/auth/mfa",
        json={"recovery_code": recovery_codes[0]},
        headers=user_headers,
    )
    assert resp.status_code == 200

    # Authenticate — should not require MFA anymore
    auth_resp = await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )
    assert auth_resp.status_code == 200
    assert auth_resp.json()["mfa_required"] is False


# =============================================================================
# MFA Recovery Code Tests (11.x)
# =============================================================================


@pytest.mark.anyio
async def test_mfa_regenerate_recovery_codes(client: AsyncClient, dbsession: Session):
    """Regenerate recovery codes invalidates old ones and creates new (11.2)."""
    email = "mfa_regen@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    _, old_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Regenerate
    resp = await client.post(
        "/v0/auth/mfa/recovery-codes",
        headers=user_headers,
    )
    assert resp.status_code == 200
    new_codes = resp.json()["recovery_codes"]
    assert len(new_codes) == 10
    assert set(new_codes) != set(old_codes)


@pytest.mark.anyio
async def test_old_recovery_codes_rejected_after_regen(
    client: AsyncClient,
    dbsession: Session,
):
    """Old recovery codes are invalid after regeneration (11.3)."""
    email = "mfa_old_codes@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    _, old_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Regenerate
    resp = await client.post(
        "/v0/auth/mfa/recovery-codes",
        headers=user_headers,
    )
    assert resp.status_code == 200

    # Try to use an old recovery code during login
    resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": old_codes[0]},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_recovery_code"


@pytest.mark.anyio
async def test_new_recovery_codes_accepted_after_regen(
    client: AsyncClient,
    dbsession: Session,
):
    """New recovery codes work after regeneration (11.4)."""
    email = "mfa_new_codes@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    # Regenerate
    resp = await client.post(
        "/v0/auth/mfa/recovery-codes",
        headers=user_headers,
    )
    new_codes = resp.json()["recovery_codes"]

    # Use a new recovery code during login
    resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": new_codes[0]},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["remaining_codes"] == 9


@pytest.mark.anyio
async def test_mfa_regenerate_when_not_enabled(
    client: AsyncClient,
    dbsession: Session,
):
    """Regeneration fails when MFA is not enabled."""
    email = "mfa_regen_none@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.post(
        "/v0/auth/mfa/recovery-codes",
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "mfa_not_enabled"


# =============================================================================
# MFA Login Verify Tests (admin endpoints — 9.x)
# =============================================================================


@pytest.mark.anyio
async def test_mfa_verify_login_correct_totp(client: AsyncClient, dbsession: Session):
    """Admin TOTP verify during login succeeds with correct code (9.1)."""
    email = "mfa_verify_ok@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    secret, _ = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Advance last_used_at to avoid replay protection
    mfa_dao = MFACredentialDAO(dbsession)
    credential = mfa_dao.get_enabled_totp(user_id)
    credential.last_used_at = datetime.now(timezone.utc) - timedelta(seconds=35)
    dbsession.flush()

    totp = pyotp.TOTP(secret)
    resp = await client.post(
        "/v0/admin/auth/mfa/verify",
        json={"user_id": user_id, "code": totp.now()},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.anyio
async def test_mfa_verify_login_wrong_totp(client: AsyncClient, dbsession: Session):
    """Admin TOTP verify during login fails with wrong code (9.2)."""
    email = "mfa_verify_bad@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    resp = await client.post(
        "/v0/admin/auth/mfa/verify",
        json={"user_id": user_id, "code": "000000"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_code"


@pytest.mark.anyio
async def test_mfa_verify_login_no_mfa(client: AsyncClient, dbsession: Session):
    """Admin TOTP verify fails when user has no MFA enabled."""
    email = "mfa_verify_nomfa@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.post(
        "/v0/admin/auth/mfa/verify",
        json={"user_id": user_id, "code": "123456"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "mfa_not_enabled"


@pytest.mark.anyio
async def test_mfa_verify_recovery_login_ok(client: AsyncClient, dbsession: Session):
    """Admin recovery code verify during login succeeds (9.6)."""
    email = "mfa_verify_rec@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    _, recovery_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": recovery_codes[0]},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["remaining_codes"] == 9


@pytest.mark.anyio
async def test_mfa_verify_recovery_reuse_rejected(
    client: AsyncClient,
    dbsession: Session,
):
    """Reused recovery code is rejected (9.8)."""
    email = "mfa_reuse_rec@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    _, recovery_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Use the first code
    resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": recovery_codes[0]},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200

    # Try to reuse the same code
    resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": recovery_codes[0]},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_recovery_code"


@pytest.mark.anyio
async def test_mfa_verify_recovery_wrong_code(
    client: AsyncClient,
    dbsession: Session,
):
    """Invalid recovery code is rejected."""
    email = "mfa_verify_rec_bad@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": "wrongcode"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_recovery_code"


@pytest.mark.anyio
async def test_mfa_verify_recovery_no_mfa(client: AsyncClient, dbsession: Session):
    """Recovery verify fails when MFA is not enabled."""
    email = "mfa_verify_rec_nomfa@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": "anycode1"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "mfa_not_enabled"


# =============================================================================
# MFA Status-by-Email Tests (admin endpoint)
# =============================================================================


@pytest.mark.anyio
async def test_mfa_status_by_email_has_mfa(client: AsyncClient, dbsession: Session):
    """Status-by-email returns mfa_enabled=True for user with MFA."""
    email = "mfa_sbe_on@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    resp = await client.get(
        f"/v0/admin/auth/mfa/status-by-email?email={email}",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_found"] is True
    assert data["mfa_enabled"] is True


@pytest.mark.anyio
async def test_mfa_status_by_email_no_mfa(client: AsyncClient, dbsession: Session):
    """Status-by-email returns mfa_enabled=False for user without MFA."""
    email = "mfa_sbe_off@example.com"
    user_id, user_headers = await _register_and_verify(client, dbsession, email)

    resp = await client.get(
        f"/v0/admin/auth/mfa/status-by-email?email={email}",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_found"] is True
    assert data["mfa_enabled"] is False


@pytest.mark.anyio
async def test_mfa_status_by_email_nonexistent(client: AsyncClient):
    """Status-by-email returns user_found=False for non-existent email."""
    resp = await client.get(
        "/v0/admin/auth/mfa/status-by-email?email=ghost@example.com",
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_found"] is False
    assert data["mfa_enabled"] is False


# =============================================================================
# Authenticate + MFA Integration Tests
# =============================================================================


@pytest.mark.anyio
async def test_authenticate_returns_mfa_required(
    client: AsyncClient,
    dbsession: Session,
):
    """Authenticate endpoint returns mfa_required=True when MFA is enabled."""
    email = "auth_mfa@example.com"
    password = "secureP@ss1"
    user_id, user_headers = await _register_and_verify(
        client,
        dbsession,
        email,
        password=password,
    )
    await _setup_and_confirm_mfa(client, dbsession, user_id, user_headers)

    resp = await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mfa_required"] is True
    assert data["email"] == email


@pytest.mark.anyio
async def test_authenticate_returns_mfa_not_required(
    client: AsyncClient,
    dbsession: Session,
):
    """Authenticate endpoint returns mfa_required=False when no MFA."""
    email = "auth_no_mfa@example.com"
    password = "secureP@ss1"
    user_id, user_headers = await _register_and_verify(
        client,
        dbsession,
        email,
        password=password,
    )

    resp = await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["mfa_required"] is False


# =============================================================================
# E2E: Full register → MFA setup → login with MFA → verify → done
# =============================================================================


@pytest.mark.anyio
async def test_e2e_register_mfa_login_verify(client: AsyncClient, dbsession: Session):
    """
    Full end-to-end flow:
      1. Register + verify (create user)
      2. Setup + confirm MFA
      3. Authenticate → mfa_required=True
      4. Verify TOTP via admin endpoint → success
      5. Also verify recovery code works
    """
    email = "e2e_mfa@example.com"
    password = "e2eP@ssword1"

    # Step 1: Register + verify
    user_id, user_headers = await _register_and_verify(
        client,
        dbsession,
        email,
        password=password,
    )

    # Step 2: Setup + confirm MFA
    secret, recovery_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Step 3: Authenticate → mfa_required
    auth_resp = await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )
    assert auth_resp.status_code == 200
    assert auth_resp.json()["mfa_required"] is True

    # Step 4: Verify TOTP
    # Advance last_used_at to avoid replay protection
    mfa_dao = MFACredentialDAO(dbsession)
    credential = mfa_dao.get_enabled_totp(user_id)
    credential.last_used_at = datetime.now(timezone.utc) - timedelta(seconds=35)
    dbsession.flush()

    totp = pyotp.TOTP(secret)
    verify_resp = await client.post(
        "/v0/admin/auth/mfa/verify",
        json={"user_id": user_id, "code": totp.now()},
        headers=ADMIN_HEADERS,
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["success"] is True

    # Step 5: Also verify a recovery code works
    rec_resp = await client.post(
        "/v0/admin/auth/mfa/verify-recovery",
        json={"user_id": user_id, "code": recovery_codes[0]},
        headers=ADMIN_HEADERS,
    )
    assert rec_resp.status_code == 200
    assert rec_resp.json()["success"] is True
    assert rec_resp.json()["remaining_codes"] == 9

    # Step 6: Check MFA status reflects enabled state
    status_resp = await client.get("/v0/auth/mfa/status", headers=user_headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["enabled"] is True
    assert status_resp.json()["recovery_codes_remaining"] == 9


@pytest.mark.anyio
async def test_e2e_mfa_setup_disable_relogin(client: AsyncClient, dbsession: Session):
    """
    E2E: Setup MFA → disable it → verify login no longer requires MFA.
    """
    email = "e2e_mfa_toggle@example.com"
    password = "toggleP@ss1"

    user_id, user_headers = await _register_and_verify(
        client,
        dbsession,
        email,
        password=password,
    )

    # Enable MFA
    _, recovery_codes = await _setup_and_confirm_mfa(
        client,
        dbsession,
        user_id,
        user_headers,
    )

    # Verify authenticate requires MFA
    auth_resp = await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )
    assert auth_resp.json()["mfa_required"] is True

    # Disable MFA
    resp = await client.request(
        "DELETE",
        "/v0/auth/mfa",
        json={"recovery_code": recovery_codes[0]},
        headers=user_headers,
    )
    assert resp.status_code == 200

    # Verify authenticate no longer requires MFA
    auth_resp2 = await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )
    assert auth_resp2.json()["mfa_required"] is False

    # MFA status should show disabled
    status_resp = await client.get("/v0/auth/mfa/status", headers=user_headers)
    assert status_resp.json()["enabled"] is False
