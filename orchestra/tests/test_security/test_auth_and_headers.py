"""
Security tests for authentication, authorization, security headers,
error message sanitization, and endpoint exposure.
"""

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import create_test_user

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unauthenticated_request_rejected(client: AsyncClient, dbsession):
    """Requests without auth header must be rejected."""
    resp = await client.get("/v0/projects")
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_invalid_api_key_rejected(client: AsyncClient, dbsession):
    """Requests with an invalid API key must be rejected."""
    resp = await client.get(
        "/v0/projects",
        headers={"Authorization": "Bearer totally_invalid_key_12345"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_empty_bearer_rejected(client: AsyncClient, dbsession):
    """Requests with empty Bearer token must be rejected."""
    resp = await client.get(
        "/v0/projects",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code in (401, 403, 422)


@pytest.mark.anyio
async def test_health_endpoint_unauthenticated(client: AsyncClient, dbsession):
    """Health check must be accessible without authentication."""
    resp = await client.get("/v0/health")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_valid_user_can_access_projects(client: AsyncClient, dbsession):
    """Sanity check: a properly authenticated user can list projects."""
    user = await create_test_user(client, "auth_sanity@test.com")
    resp = await client.get("/v0/projects", headers=user["headers"])
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Admin authorization
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_regular_key_cannot_access_admin(client: AsyncClient, dbsession):
    """A regular user API key must NOT grant access to admin endpoints."""
    user = await create_test_user(client, "non_admin@test.com")
    resp = await client.get(
        "/v0/admin/user/by-user-id?user_id=fake",
        headers=user["headers"],
    )
    assert resp.status_code in (
        401,
        403,
    ), f"Regular user accessed admin endpoint (got {resp.status_code})"


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_security_headers_present(client: AsyncClient, dbsession):
    """All required security headers must be present on responses."""
    resp = await client.get("/v0/health")

    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "max-age=" in (resp.headers.get("strict-transport-security") or "")
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in (resp.headers.get("permissions-policy") or "")


@pytest.mark.anyio
async def test_csp_header_present(client: AsyncClient, dbsession):
    """Content-Security-Policy header must be present."""
    resp = await client.get("/v0/health")
    csp = resp.headers.get("content-security-policy")
    assert csp is not None, "Content-Security-Policy header is missing"
    assert "default-src" in csp


# ---------------------------------------------------------------------------
# OpenAPI / Swagger exposure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_openapi_not_exposed(client: AsyncClient, dbsession):
    """OpenAPI/Swagger/Redoc endpoints must not be publicly accessible."""
    for path in ["/openapi.json", "/redoc"]:
        resp = await client.get(path)
        assert resp.status_code == 404, f"{path} is accessible (got {resp.status_code})"


# ---------------------------------------------------------------------------
# Error message sanitization
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_malformed_json_no_stack_trace(client: AsyncClient, dbsession):
    """Malformed JSON must return a clean error, not a Python traceback."""
    user = await create_test_user(client, "malformed_json@test.com")
    resp = await client.post(
        "/v0/project",
        content=b"{not valid json",
        headers={**user["headers"], "Content-Type": "application/json"},
    )
    body = resp.text
    assert "Traceback" not in body
    assert 'File "/' not in body


@pytest.mark.anyio
async def test_sql_like_input_no_leak(client: AsyncClient, dbsession):
    """SQL-like payloads in query params must not leak DB details."""
    user = await create_test_user(client, "sqli_test@test.com")
    resp = await client.get(
        "/v0/interfaces/?interface_id=' OR 1=1--",
        headers=user["headers"],
    )
    body = resp.text.lower()
    assert "postgresql" not in body
    assert "sqlalchemy" not in body
    assert "traceback" not in body


# ---------------------------------------------------------------------------
# HTTP method restrictions
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trace_method_rejected(client: AsyncClient, dbsession):
    """TRACE method must be rejected."""
    resp = await client.request("TRACE", "/v0/health")
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Stripe webhook signature
# ---------------------------------------------------------------------------


FAKE_STRIPE_SECRET_KEY = "sk_test_fake_key_for_testing"
FAKE_STRIPE_WEBHOOK_SECRET = "whsec_test_fake_webhook_secret"


def _stripe_settings_patch():
    """Patch settings so the handler reaches signature verification."""
    return patch.multiple(
        "orchestra.web.api.webhooks.stripe.settings",
        stripe_secret_key=FAKE_STRIPE_SECRET_KEY,
        stripe_webhook_secret=FAKE_STRIPE_WEBHOOK_SECRET,
        stripe_skip_signature_verification=False,
    )


@pytest.mark.anyio
async def test_stripe_webhook_rejects_forged_signature(
    client: AsyncClient,
    dbsession,
):
    """Forged Stripe-Signature header must be rejected with 400."""
    with _stripe_settings_patch():
        resp = await client.post(
            "/v0/webhooks/stripe",
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=1234567890,v1=fakesig",
            },
            content=b'{"id":"evt_fake","type":"checkout.session.completed"}',
        )
    assert (
        resp.status_code == 400
    ), f"Forged Stripe webhook was not rejected (got {resp.status_code})"


@pytest.mark.anyio
async def test_stripe_webhook_rejects_missing_signature(
    client: AsyncClient,
    dbsession,
):
    """Stripe webhook without Stripe-Signature header must be rejected with 400."""
    with _stripe_settings_patch():
        resp = await client.post(
            "/v0/webhooks/stripe",
            headers={"Content-Type": "application/json"},
            content=b'{"id":"evt_fake","type":"checkout.session.completed"}',
        )
    assert (
        resp.status_code == 400
    ), f"Missing signature was not rejected (got {resp.status_code})"
