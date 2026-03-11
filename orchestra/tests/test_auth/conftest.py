"""
Shared fixtures and helpers for auth tests.

Provides:
- Constants for admin headers and patch targets
- User lifecycle helpers: register, verify, authenticate, register_and_verify
- OAuth user setup helper
- MFA setup helper
- E2E helpers for tests against a running Orchestra instance
"""

import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from unittest.mock import AsyncMock, patch

import httpx
import pyotp
from httpx import AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from orchestra.db.dao.auth_dao import AuthDAO, decrypt_secret, hash_code

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
    "Content-Type": "application/json",
}

EMAIL_PATCH_TARGET = "orchestra.web.api.utils.email.send_email_async"
TURNSTILE_PATCH_TARGET = "orchestra.web.api.auth.views.verify_turnstile_token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def register(
    client: AsyncClient,
    email: str,
    password: str = "secureP@ss1",
    name: str = "Test",
    last_name: str = "User",
    *,
    captcha_token: str | None = None,
):
    """POST /admin/auth/register."""
    payload = {
        "email": email,
        "password": password,
        "name": name,
        "last_name": last_name,
    }
    if captcha_token is not None:
        payload["captcha_token"] = captcha_token
    return await client.post(
        "/v0/admin/auth/register",
        json=payload,
        headers=ADMIN_HEADERS,
    )


async def verify_code(
    client: AsyncClient,
    email: str,
    code: str,
    purpose: str = "signup",
):
    """POST /admin/auth/verify-code — returns the raw response."""
    return await client.post(
        "/v0/admin/auth/verify-code",
        json={"email": email, "code": code, "purpose": purpose},
        headers=ADMIN_HEADERS,
    )


async def verify(client: AsyncClient, email: str, code: str):
    """Verify a signup code and create the user (two-step)."""
    resp = await verify_code(client, email, code, purpose="signup")
    if resp.status_code != 200:
        return resp
    token = resp.json()["token"]
    return await client.post(
        "/v0/admin/auth/create-user",
        json={"token": token},
        headers=ADMIN_HEADERS,
    )


async def authenticate(client: AsyncClient, email: str, password: str):
    """POST /admin/auth/authenticate."""
    return await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )


async def forgot_password(
    client: AsyncClient,
    email: str,
    *,
    captcha_token: str | None = None,
):
    """POST /admin/auth/forgot-password."""
    payload: dict = {"email": email}
    if captcha_token is not None:
        payload["captcha_token"] = captcha_token
    return await client.post(
        "/v0/admin/auth/forgot-password",
        json=payload,
        headers=ADMIN_HEADERS,
    )


async def register_and_verify(
    client: AsyncClient,
    dbsession: Session,
    email: str,
    password: str = "secureP@ss1",
    name: str = "Test",
    last_name: str = "User",
) -> tuple[str, dict]:
    """
    Full signup: register ‑> set known code ‑> verify ‑> create user.

    Mocks email sending and Turnstile internally so it works in any
    environment.  Returns ``(user_id, user_api_headers)``.
    """
    from orchestra.db.dao.api_key_dao import ApiKeyDAO
    from orchestra.db.dao.user_dao import UserDAO

    with (
        patch(EMAIL_PATCH_TARGET, new_callable=AsyncMock) as mock_send,
        patch(TURNSTILE_PATCH_TARGET, new_callable=AsyncMock) as mock_captcha,
    ):
        mock_send.return_value = True
        mock_captcha.return_value = True
        resp = await register(client, email, password, name, last_name)
        assert resp.status_code == 200, resp.json()

    # Overwrite the hashed code with a known value
    dao = AuthDAO(dbsession)
    entry = dao.get_pending_verification(email, "signup")
    assert entry is not None, f"No pending signup verification for {email}"
    code = "123456"
    entry.code_hash = hash_code(code)
    dbsession.flush()

    resp = await verify(client, email, code)
    assert resp.status_code == 200, resp.json()
    user_id = resp.json()["id"]

    # Build API-key headers for the newly created user
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


async def create_oauth_user(
    client: AsyncClient,
    dbsession: Session,
    email: str,
    provider: str = "google",
) -> tuple[str, dict]:
    """
    Create an OAuth-only user (no email/password).

    Returns ``(user_id, user_api_headers)``.
    """
    from orchestra.db.dao.api_key_dao import ApiKeyDAO
    from orchestra.db.dao.user_dao import UserDAO

    resp = await client.post(
        "/v0/admin/user",
        json={"email": email},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    user_id = resp.json()["id"]

    await client.post(
        "/v0/admin/auth/account",
        json={
            "provider": provider,
            "type": "oauth",
            "provider_account_id": f"{provider}-{email}",
            "access_token": "token",
            "expires_at": 9999999999,
            "scope": "openid",
            "token_type": "Bearer",
            "id_token": "id_token",
            "user_id": user_id,
        },
        headers=ADMIN_HEADERS,
    )

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


async def setup_and_confirm_mfa(
    client: AsyncClient,
    dbsession: Session,
    user_id: str,
    user_headers: dict,
) -> tuple[str, list[str]]:
    """
    Run MFA setup + confirm for a user.

    Returns ``(totp_secret, recovery_codes)``.
    """
    resp = await client.post("/v0/auth/mfa/setup", headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["qr_code_uri"].startswith("otpauth://totp/")

    mfa_dao = AuthDAO(dbsession)
    credential = mfa_dao.get_pending_totp(user_id)
    assert credential is not None
    secret = decrypt_secret(credential.credential_data)

    totp = pyotp.TOTP(secret)
    resp = await client.post(
        "/v0/auth/mfa/confirm",
        json={"code": totp.now()},
        headers=user_headers,
    )
    assert resp.status_code == 200
    recovery_codes = resp.json()["recovery_codes"]
    assert len(recovery_codes) == 10

    return secret, recovery_codes


def advance_totp_window(dbsession: Session, user_id: str):
    """Push last_used_at back so the next TOTP code is accepted."""
    dao = AuthDAO(dbsession)
    credential = dao.get_enabled_totp(user_id)
    credential.last_used_at = datetime.now(timezone.utc) - timedelta(seconds=35)
    dbsession.flush()
    return credential


# ---------------------------------------------------------------------------
# E2E Helpers — for tests against a running Orchestra instance
# ---------------------------------------------------------------------------
#
# Used by test_auth_flows.py which hits a live server over HTTP rather than
# the in-process ASGI test client.  DB access is used only for test setup
# (setting known verification codes, reading TOTP secrets) — never for
# assertions on the system's behaviour.
#

E2E_BASE_URL = os.getenv("ORCHESTRA_E2E_URL", "http://localhost:8000")
E2E_DB_URL = os.getenv(
    "ORCHESTRA_DB_URL",
    "postgresql://orchestra:orchestra@localhost:5432/orchestra",
)


def e2e_server_reachable() -> bool:
    """Return True when the Orchestra server is accepting requests."""
    try:
        resp = httpx.get(f"{E2E_BASE_URL}/v0/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def unique_email(prefix: str = "e2e") -> str:
    """Generate a unique test email to avoid cross-run collisions."""
    return f"{prefix}-{int(time.time())}-{secrets.token_hex(3)}@example.com"


@lru_cache(maxsize=1)
def _e2e_engine():
    return create_engine(E2E_DB_URL)


def e2e_db_scalar(sql: str, params: dict | None = None):
    """Run a read query and return the first column of the first row."""
    with _e2e_engine().connect() as conn:
        row = conn.execute(text(sql), params or {}).fetchone()
        return row[0] if row else None


def e2e_db_execute(sql: str, params: dict | None = None):
    """Run a write query and commit."""
    with _e2e_engine().connect() as conn:
        conn.execute(text(sql), params or {})
        conn.commit()


# ── E2E HTTP helpers ──────────────────────────────────────────────────────


async def e2e_register(
    client: httpx.AsyncClient,
    email: str,
    password: str = "secureP@ss1",
    name: str = "Test",
    last_name: str = "User",
):
    """POST /admin/auth/register against the real server."""
    return await client.post(
        "/v0/admin/auth/register",
        json={
            "email": email,
            "password": password,
            "name": name,
            "last_name": last_name,
        },
        headers=ADMIN_HEADERS,
    )


async def e2e_verify_code(
    client: httpx.AsyncClient,
    email: str,
    code: str,
    purpose: str = "signup",
):
    """POST /admin/auth/verify-code against the real server."""
    return await client.post(
        "/v0/admin/auth/verify-code",
        json={"email": email, "code": code, "purpose": purpose},
        headers=ADMIN_HEADERS,
    )


async def e2e_verify(
    client: httpx.AsyncClient,
    email: str,
    code: str,
):
    """Verify a signup code and create the user (two-step) via real HTTP."""
    resp = await e2e_verify_code(client, email, code, purpose="signup")
    if resp.status_code != 200:
        return resp
    token = resp.json()["token"]
    return await client.post(
        "/v0/admin/auth/create-user",
        json={"token": token},
        headers=ADMIN_HEADERS,
    )


async def e2e_authenticate(
    client: httpx.AsyncClient,
    email: str,
    password: str,
):
    """POST /admin/auth/authenticate against the real server."""
    return await client.post(
        "/v0/admin/auth/authenticate",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )


async def e2e_forgot_password(
    client: httpx.AsyncClient,
    email: str,
):
    """POST /admin/auth/forgot-password against the real server."""
    return await client.post(
        "/v0/admin/auth/forgot-password",
        json={"email": email},
        headers=ADMIN_HEADERS,
    )


async def e2e_register_and_verify(
    client: httpx.AsyncClient,
    email: str,
    password: str = "secureP@ss1",
    name: str = "Test",
    last_name: str = "User",
) -> tuple[str, dict]:
    """
    Full signup via real HTTP: register → set known code → verify → create user.

    Returns ``(user_id, user_api_headers)``.
    """
    resp = await e2e_register(client, email, password, name, last_name)
    assert resp.status_code == 200, f"Registration failed: {resp.text}"

    # Overwrite the hashed code with a known value
    code = "123456"
    e2e_db_execute(
        "UPDATE email_verification "
        "SET code_hash = :hash, attempts = 0 "
        "WHERE email = :email AND purpose = 'signup'",
        {"hash": hash_code(code), "email": email},
    )

    resp = await e2e_verify(client, email, code)
    assert resp.status_code == 200, f"Verification failed: {resp.text}"
    user_id = resp.json()["id"]

    # Read API key from DB
    api_key = e2e_db_scalar(
        "SELECT ak.key FROM api_key ak "
        'JOIN "user" u ON ak.user_id = u.id '
        "WHERE u.email = :email LIMIT 1",
        {"email": email},
    )
    assert api_key, f"No API key found for {email}"

    user_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return user_id, user_headers


async def e2e_create_oauth_user(
    client: httpx.AsyncClient,
    email: str,
    provider: str = "google",
) -> tuple[str, dict]:
    """Create an OAuth-only user via real HTTP. Returns ``(user_id, user_api_headers)``."""
    resp = await client.post(
        "/v0/admin/user",
        json={"email": email},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    user_id = resp.json()["id"]

    await client.post(
        "/v0/admin/auth/account",
        json={
            "provider": provider,
            "type": "oauth",
            "provider_account_id": f"{provider}-{email}",
            "access_token": "token",
            "expires_at": 9999999999,
            "scope": "openid",
            "token_type": "Bearer",
            "id_token": "id_token",
            "user_id": user_id,
        },
        headers=ADMIN_HEADERS,
    )

    api_key = e2e_db_scalar(
        "SELECT ak.key FROM api_key ak "
        'JOIN "user" u ON ak.user_id = u.id '
        "WHERE u.email = :email LIMIT 1",
        {"email": email},
    )
    assert api_key, f"No API key found for {email}"

    user_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return user_id, user_headers


async def e2e_setup_and_confirm_mfa(
    client: httpx.AsyncClient,
    user_id: str,
    user_headers: dict,
) -> tuple[str, list[str]]:
    """
    Run MFA setup + confirm via real HTTP.

    Returns ``(totp_secret, recovery_codes)``.
    """
    resp = await client.post("/v0/auth/mfa/setup", headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["qr_code_uri"].startswith("otpauth://totp/")

    # Read the pending TOTP secret from DB
    encrypted = e2e_db_scalar(
        "SELECT credential_data FROM mfa_credential "
        "WHERE user_id = :uid AND confirmed_at IS NULL",
        {"uid": user_id},
    )
    assert encrypted is not None, "No pending TOTP credential found"
    # DB driver may return memoryview for BYTEA columns; decrypt_secret needs bytes
    if isinstance(encrypted, memoryview):
        encrypted = bytes(encrypted)
    secret = decrypt_secret(encrypted)

    totp = pyotp.TOTP(secret)
    resp = await client.post(
        "/v0/auth/mfa/confirm",
        json={"code": totp.now()},
        headers=user_headers,
    )
    assert resp.status_code == 200
    recovery_codes = resp.json()["recovery_codes"]
    assert len(recovery_codes) == 10

    return secret, recovery_codes


def e2e_advance_totp_window(user_id: str):
    """Push last_used_at back so the next TOTP code is accepted."""
    e2e_db_execute(
        "UPDATE mfa_credential "
        "SET last_used_at = NOW() - INTERVAL '35 seconds' "
        "WHERE user_id = :uid AND enabled = true",
        {"uid": user_id},
    )
