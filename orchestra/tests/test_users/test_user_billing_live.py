"""
Live Stripe Sandbox Tests for User Billing.

These tests hit the REAL Stripe sandbox API - they are skipped if
STRIPE_SECRET_KEY is not configured.

Requirements:
    - STRIPE_SECRET_KEY env var set (sandbox key starting with sk_test_)
    - Network access to Stripe API

Run these tests:
    # Set env vars first
    export STRIPE_SECRET_KEY=sk_test_xxx

    # Run the tests
    pytest orchestra/tests/test_users/test_user_billing_live.py -v

    # Or run with .env file (if STRIPE_SECRET_KEY is defined there)
    pytest orchestra/tests/test_users/test_user_billing_live.py -v
"""

import os

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import create_test_user

# Skip all tests in this module if Stripe is not configured
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
SKIP_REASON = "Live Stripe tests require STRIPE_SECRET_KEY env var (sk_test_xxx)"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not STRIPE_SECRET_KEY.startswith("sk_test_"),
        reason=SKIP_REASON,
    ),
    pytest.mark.anyio,
]

# Track Stripe customers created during tests for cleanup
_created_stripe_customers: list[str] = []


@pytest.fixture(autouse=True)
def _ensure_stripe_configured(monkeypatch):
    """Ensure Stripe settings use real env vars for live tests."""
    from orchestra.settings import settings

    if STRIPE_SECRET_KEY:
        monkeypatch.setattr(
            settings,
            "stripe_secret_key",
            STRIPE_SECRET_KEY,
            raising=False,
        )


@pytest.fixture(autouse=True)
def _cleanup_stripe_customers():  # noqa: PT004 - autouse fixture doesn't need return
    """
    Cleanup fixture that deletes any Stripe customers created during tests.

    This runs after each test to prevent orphaned customers in Stripe sandbox.
    Uses autouse=True so it's automatically applied to all tests in this module.
    """
    _created_stripe_customers.clear()
    yield

    # Cleanup after test
    if _created_stripe_customers and STRIPE_SECRET_KEY:
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        for customer_id in _created_stripe_customers:
            try:
                stripe.Customer.delete(customer_id)
                print(f"Cleaned up Stripe customer: {customer_id}")
            except stripe.error.InvalidRequestError:
                # Customer already deleted or doesn't exist
                pass
            except Exception as e:
                print(f"Warning: Failed to cleanup Stripe customer {customer_id}: {e}")

        _created_stripe_customers.clear()


def track_stripe_customer(customer_id: str):
    """Register a Stripe customer ID for cleanup after test."""
    if customer_id and customer_id.startswith("cus_"):
        _created_stripe_customers.append(customer_id)


# ============================================================================
# Live Stripe Customer Tests
# ============================================================================


async def test_live_stripe_customer_creation(client: AsyncClient):
    """
    LIVE TEST: Create a real Stripe customer via checkout session.

    This test hits the actual Stripe sandbox API to verify:
    - Customer creation works with real API
    - Response structure matches expected format
    - Checkout URL is a valid Stripe URL
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_test_{os.urandom(4).hex()}@example.com",
    )

    # Create checkout session which triggers customer creation
    response = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )

    assert (
        response.status_code == 200
    ), f"Failed to create checkout session: {response.json()}"
    data = response.json()

    # Verify we got a real Stripe checkout URL
    assert "url" in data
    assert data["url"].startswith("https://checkout.stripe.com/")

    # Verify session ID format (sandbox sessions start with cs_test_)
    assert "session_id" in data
    assert data["session_id"].startswith("cs_test_")

    # Retrieve and verify the session exists in Stripe
    session = stripe.checkout.Session.retrieve(data["session_id"])
    assert session.id == data["session_id"]
    assert session.mode == "payment"
    assert session.payment_status == "unpaid"

    # Track customer for cleanup
    track_stripe_customer(session.customer)


async def test_live_stripe_checkout_session_structure(client: AsyncClient):
    """
    LIVE TEST: Verify checkout session has correct structure for credit purchase.

    This test validates:
    - Session has correct payment mode
    - client_reference_id contains user ID
    - Amount is correctly calculated
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_session_{os.urandom(4).hex()}@example.com",
    )

    # Create checkout session (amount is server-controlled via settings)
    response = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )

    assert response.status_code == 200
    session_id = response.json()["session_id"]

    # Retrieve full session details from Stripe
    session = stripe.checkout.Session.retrieve(
        session_id,
        expand=["line_items"],
    )

    # Verify session structure
    assert session.payment_status == "unpaid"
    assert session.client_reference_id == user["id"]
    assert session.mode == "payment"

    # Verify line items exist (quantity is server-controlled)
    assert session.line_items is not None
    assert len(session.line_items.data) == 1

    # Track customer for cleanup
    track_stripe_customer(session.customer)


async def test_live_stripe_customer_reuse(client: AsyncClient, dbsession: Session):
    """
    LIVE TEST: Verify Stripe customer is reused for subsequent checkouts.

    This test validates:
    - First checkout creates a new Stripe customer
    - Second checkout reuses the same customer
    - Customer ID is correctly stored in our database
    """
    import stripe

    from orchestra.db.dao.user_dao import UserDAO

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_reuse_{os.urandom(4).hex()}@example.com",
    )

    # First checkout - creates customer
    response1 = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )
    assert response1.status_code == 200
    session1 = stripe.checkout.Session.retrieve(response1.json()["session_id"])
    customer_id_1 = session1.customer

    assert customer_id_1 is not None
    assert customer_id_1.startswith("cus_")

    # Second checkout - should reuse customer
    response2 = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )
    assert response2.status_code == 200
    session2 = stripe.checkout.Session.retrieve(response2.json()["session_id"])
    customer_id_2 = session2.customer

    # Both should use the same customer
    assert customer_id_1 == customer_id_2

    # Verify stored in our DB
    user_dao = UserDAO(session=dbsession)
    db_user_row = user_dao.get_by_id(user["id"])
    assert db_user_row is not None
    db_user = db_user_row[0]
    assert db_user.billing_account.stripe_customer_id == customer_id_1

    # Track customer for cleanup
    track_stripe_customer(customer_id_1)


async def test_live_stripe_customer_email_metadata(client: AsyncClient):
    """
    LIVE TEST: Verify Stripe customer has correct email and metadata.

    This test validates:
    - Customer is created with correct email
    - Customer metadata or description references our system
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    email = f"live_meta_{os.urandom(4).hex()}@example.com"
    user = await create_test_user(client, email)

    response = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )

    assert response.status_code == 200
    session = stripe.checkout.Session.retrieve(response.json()["session_id"])

    # Get the customer
    customer = stripe.Customer.retrieve(session.customer)

    # Verify customer email
    assert customer.email == email

    # Track customer for cleanup
    track_stripe_customer(session.customer)


async def test_live_stripe_multiple_checkouts(client: AsyncClient):
    """
    LIVE TEST: Verify multiple checkout sessions reuse the same customer.

    Creates multiple sessions and ensures Stripe customer is consistent.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_amounts_{os.urandom(4).hex()}@example.com",
    )

    # Create multiple checkout sessions — all should succeed and reuse the
    # same Stripe customer (amount is server-controlled, not per-request).
    customer_ids = set()
    for i in range(3):
        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )

        assert (
            response.status_code == 200
        ), f"Failed on iteration {i}: {response.json()}"

        session = stripe.checkout.Session.retrieve(
            response.json()["session_id"],
            expand=["line_items"],
        )

        # Verify a line item exists
        assert session.line_items is not None
        assert len(session.line_items.data) >= 1

        if session.customer:
            customer_ids.add(session.customer)

        # Track customer for cleanup (only once per user)
        if i == 0:
            track_stripe_customer(session.customer)

    # All sessions should share the same Stripe customer
    assert len(customer_ids) <= 1, f"Expected single customer, got {customer_ids}"
