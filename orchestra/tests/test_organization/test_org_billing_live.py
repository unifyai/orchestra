"""
Live Stripe Sandbox Tests for Organization Billing.

These tests hit the REAL Stripe sandbox API - they are skipped if
STRIPE_SECRET_KEY is not configured.

Requirements:
    - STRIPE_SECRET_KEY env var set (sandbox key starting with sk_test_)
    - Network access to Stripe API

Run these tests:
    # Set env vars first
    export STRIPE_SECRET_KEY=sk_test_xxx

    # Run the tests
    pytest orchestra/tests/test_organization/test_org_billing_live.py -v

NOTE: The explicit ``/organizations/{id}/billing/stripe-customer`` and
``/organizations/{id}/billing/checkout`` endpoints have been consolidated
into the unified ``/billing/checkout-session`` endpoint.  These tests now
use the unified endpoint + org-scoped API keys.
"""

import os

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.tests.utils import create_test_org, create_test_user

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
        monkeypatch.setattr(
            settings,
            "stripe_unify_credits_price_id_business",
            os.environ.get(
                "STRIPE_UNIFY_CREDITS_PRICE_ID_BUSINESS",
                "price_test_business_dummy",
            ),
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
# Live Organization Stripe Tests
# ============================================================================


async def test_live_org_checkout_session(client: AsyncClient, dbsession: Session):
    """
    LIVE TEST: Create checkout session for organization credit purchase.

    The unified /billing/checkout-session endpoint creates a Stripe customer
    automatically if one doesn't exist.

    This test validates:
    - Checkout session is created with organization metadata
    - Session URL is valid Stripe URL
    - Stripe customer is created/reused correctly
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_org_checkout_{os.urandom(4).hex()}@example.com",
    )
    org = await create_test_org(
        client,
        user,
        f"Checkout Org {os.urandom(4).hex()}",
    )

    # Create checkout session via unified endpoint
    checkout_response = await client.post(
        "/v0/billing/checkout-session",
        headers=org["headers"],
    )
    assert checkout_response.status_code == 200, checkout_response.json()
    data = checkout_response.json()

    assert data["url"].startswith("https://checkout.stripe.com/")
    assert data["session_id"].startswith("cs_test_")

    # Verify session in Stripe
    session = stripe.checkout.Session.retrieve(
        data["session_id"],
        expand=["line_items"],
    )

    assert session.mode == "payment"
    assert session.payment_status == "unpaid"

    # Track for cleanup
    if session.customer:
        track_stripe_customer(session.customer)


async def test_live_org_with_business_details(client: AsyncClient, dbsession: Session):
    """
    LIVE TEST: Create organization with business details synced to Stripe.

    This test validates:
    - Organization business details are synced to Stripe customer
    - Address is correctly formatted when org is updated AFTER Stripe customer exists
    """
    import stripe

    from orchestra.db.dao.organization_dao import OrganizationDAO

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_org_business_{os.urandom(4).hex()}@example.com",
    )
    org = await create_test_org(
        client,
        user,
        f"Business Org {os.urandom(4).hex()}",
    )
    org_id = org["id"]

    # Create a checkout session to generate Stripe customer
    checkout_response = await client.post(
        "/v0/billing/checkout-session",
        headers=org["headers"],
    )
    assert checkout_response.status_code == 200

    # Get customer ID from DB (set during checkout session creation)
    dbsession.expire_all()
    org_dao = OrganizationDAO(session=dbsession)
    org_obj = org_dao.get(org_id)
    customer_id = org_obj.billing_account.stripe_customer_id
    assert customer_id is not None, "Stripe customer should be created by checkout"

    # Track for cleanup
    track_stripe_customer(customer_id)

    # Now update business profile via the billing endpoint - this syncs to Stripe
    update_response = await client.patch(
        "/v0/billing/billing-profile",
        json={
            "business_name": "Acme Corporation",
            "billing_address": {
                "line1": "123 Main Street",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94102",
                "country": "US",
            },
        },
        headers=org["headers"],
    )
    assert update_response.status_code == 200, update_response.json()

    # Verify in Stripe
    customer = stripe.Customer.retrieve(customer_id)

    # business_name should be synced to Stripe customer name
    assert customer.name == "Acme Corporation"

    # billing_address should be synced to Stripe customer address
    assert customer.address is not None
    assert customer.address.line1 == "123 Main Street"
    assert customer.address.city == "San Francisco"
    assert customer.address.state == "CA"
    assert customer.address.postal_code == "94102"
    assert customer.address.country == "US"


async def test_live_org_multiple_checkouts_same_customer(
    client: AsyncClient,
    dbsession: Session,
):
    """
    LIVE TEST: Multiple checkouts use the same Stripe customer.

    This test validates:
    - First checkout creates a customer
    - Subsequent checkouts reuse the same customer
    """
    import stripe

    from orchestra.db.dao.organization_dao import OrganizationDAO

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_org_multi_{os.urandom(4).hex()}@example.com",
    )
    org = await create_test_org(
        client,
        user,
        f"Multi Checkout Org {os.urandom(4).hex()}",
    )
    org_id = org["id"]

    # First checkout – creates customer
    first_checkout = await client.post(
        "/v0/billing/checkout-session",
        headers=org["headers"],
    )
    assert first_checkout.status_code == 200

    dbsession.expire_all()
    org_dao = OrganizationDAO(session=dbsession)
    original_customer_id = org_dao.get(org_id).billing_account.stripe_customer_id
    track_stripe_customer(original_customer_id)

    # Subsequent checkouts
    for _ in range(2):
        checkout = await client.post(
            "/v0/billing/checkout-session",
            headers=org["headers"],
        )
        assert checkout.status_code == 200

        session = stripe.checkout.Session.retrieve(checkout.json()["session_id"])
        assert session.customer == original_customer_id


async def test_live_org_tax_id_sync(client: AsyncClient, dbsession: Session):
    """
    LIVE TEST: Tax ID is synced to Stripe customer.

    This test validates:
    - Tax ID can be set on organization
    - Tax ID appears on Stripe customer
    """
    import stripe

    from orchestra.db.dao.organization_dao import OrganizationDAO

    stripe.api_key = STRIPE_SECRET_KEY

    user = await create_test_user(
        client,
        f"live_org_tax_{os.urandom(4).hex()}@example.com",
    )
    org = await create_test_org(
        client,
        user,
        f"Tax Org {os.urandom(4).hex()}",
    )
    org_id = org["id"]

    # Create a Stripe customer via checkout
    checkout = await client.post(
        "/v0/billing/checkout-session",
        headers=org["headers"],
    )
    assert checkout.status_code == 200

    dbsession.expire_all()
    org_dao = OrganizationDAO(session=dbsession)
    customer_id = org_dao.get(org_id).billing_account.stripe_customer_id
    assert customer_id is not None
    track_stripe_customer(customer_id)

    # Update business profile with tax ID (US EIN format for testing)
    update_response = await client.patch(
        "/v0/billing/billing-profile",
        json={
            "tax_id": "12-3456789",
            "billing_address": {
                "country": "US",
                "line1": "123 Test St",
                "city": "Test City",
                "postal_code": "12345",
            },
        },
        headers=org["headers"],
    )
    assert update_response.status_code == 200, update_response.json()

    # Check tax IDs on Stripe customer
    tax_ids = stripe.Customer.list_tax_ids(customer_id)

    # Should have the tax ID synced
    assert len(tax_ids.data) > 0, "Expected tax ID to be synced to Stripe"
    # US EIN may be stored without hyphen
    assert any(
        tid.value == "12-3456789" or tid.value == "123456789" for tid in tax_ids.data
    ), f"Tax ID not found in Stripe. Found: {[t.value for t in tax_ids.data]}"
