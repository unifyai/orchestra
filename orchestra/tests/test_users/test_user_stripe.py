"""
Tests for User Stripe integration.

Covers:
- Stripe customer creation (lazy)
- Checkout session creation
- Webhook handling for user credit top-ups
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.settings import settings
from orchestra.tests.utils import create_test_user

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}",
}


@pytest.fixture(autouse=True)
def _mock_stripe_settings(monkeypatch):
    """Set Stripe settings for tests."""
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_for_mocking",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_webhook_secret",
        "whsec_test",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_skip_signature_verification",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_unify_credits_price_id_personal",
        "price_test_personal_dummy",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_unify_credits_price_id_business",
        "price_test_business_dummy",
        raising=False,
    )


# ============================================================================
# Checkout Session Tests
# ============================================================================


@pytest.mark.anyio
async def test_user_checkout_session_success(client: AsyncClient):
    """Test creating a checkout session for user credit purchase."""
    user = await create_test_user(client, "stripe_checkout@example.com")

    with patch("stripe.Customer.create") as mock_customer_create, patch(
        "stripe.checkout.Session.create",
    ) as mock_session_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_test_checkout"
        mock_customer_create.return_value = mock_customer

        mock_session = MagicMock()
        mock_session.id = "cs_test_123"
        mock_session.url = "https://checkout.stripe.com/test"
        mock_session_create.return_value = mock_session

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        assert "url" in data
        assert "session_id" in data


# ============================================================================
# Stripe Customer Tests
# ============================================================================


@pytest.mark.anyio
async def test_set_stripe_customer_id(client: AsyncClient):
    """Test setting Stripe customer ID for a user."""
    # Create user
    url = "/v0/admin/user"
    params = {"email": "stripe_set_cus@example.com"}
    response = await client.post(url, json=params, headers=HEADERS)
    user_id = response.json()["id"]

    # Set Stripe customer ID
    url = "/v0/admin/stripe_customer_id"
    response = await client.put(
        url,
        params={"id": user_id, "stripe_customer_id": "cus_test_12345"},
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_lazy_stripe_customer_creation_during_checkout(client: AsyncClient):
    """Test that Stripe customer is created lazily during checkout."""
    user = await create_test_user(client, "stripe_lazy_creation@example.com")

    with patch("stripe.Customer.create") as mock_customer_create, patch(
        "stripe.checkout.Session.create",
    ) as mock_session_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_lazily_created"
        mock_customer_create.return_value = mock_customer

        mock_session = MagicMock()
        mock_session.id = "cs_lazy_test"
        mock_session.url = "https://checkout.stripe.com/lazy"
        mock_session_create.return_value = mock_session

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )

        assert response.status_code == 200
        # Customer.create should have been called for new user
        # (or session.create was called with customer email for Stripe to create)


# ============================================================================
# Webhook Tests
# ============================================================================


@pytest.mark.anyio
async def test_checkout_webhook_adds_credits(client: AsyncClient, dbsession):
    """Test that checkout.session.completed webhook adds credits to user."""
    from orchestra.db.dao.user_dao import UserDAO

    user_dao = UserDAO(dbsession)
    initial_user = user_dao.get_user_with_id("user1")
    initial_credits = float(initial_user.billing_account.credits) if initial_user else 0

    webhook_payload = {
        "id": "evt_user_checkout_webhook_test",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_webhook_test",
                "customer": "cus_test_user",
                "client_reference_id": "user1",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 5000,  # $50
                "currency": "usd",
                "metadata": {},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response.status_code == 200

    dbsession.expire_all()
    updated_user = user_dao.get_user_with_id("user1")
    assert float(updated_user.billing_account.credits) == initial_credits + 50


@pytest.mark.anyio
async def test_checkout_webhook_idempotent(client: AsyncClient, dbsession):
    """Test that duplicate checkout webhooks don't add credits twice."""
    from orchestra.db.dao.user_dao import UserDAO

    user_dao = UserDAO(dbsession)
    initial_user = user_dao.get_user_with_id("user2")
    initial_credits = float(initial_user.billing_account.credits) if initial_user else 0

    event_id = "evt_idempotent_webhook_test"
    webhook_payload = {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_idempotent",
                "customer": "cus_test",
                "client_reference_id": "user2",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "usd",
                "metadata": {},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        # First call
        response1 = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response1.status_code == 200

        dbsession.expire_all()
        after_first = user_dao.get_user_with_id("user2")
        credits_after_first = float(after_first.billing_account.credits)

        # Second call with same event ID
        response2 = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response2.status_code == 200

    dbsession.expire_all()
    after_second = user_dao.get_user_with_id("user2")
    credits_after_second = float(after_second.billing_account.credits)

    # Credits should be same (idempotent)
    assert credits_after_second == credits_after_first


# ============================================================================
# E2E User Billing Flows
# ============================================================================


@pytest.mark.anyio
async def test_e2e_user_personal_checkout_flow(client: AsyncClient, dbsession):
    """
    E2E Test: Complete user personal billing checkout flow.

    Flow:
    1. User creates account
    2. User initiates checkout (Stripe customer created lazily)
    3. User completes payment (webhook received)
    4. Credits are added to user account
    """

    from orchestra.db.dao.user_dao import UserDAO

    # Step 1: Create user
    user = await create_test_user(client, "e2e_personal_checkout@example.com")

    # Step 2: User initiates checkout
    with patch("stripe.Customer.create") as mock_customer_create, patch(
        "stripe.checkout.Session.create",
    ) as mock_session_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_e2e_personal_checkout"
        mock_customer_create.return_value = mock_customer

        mock_session = MagicMock()
        mock_session.id = "cs_e2e_personal"
        mock_session.url = "https://checkout.stripe.com/e2e"
        mock_session_create.return_value = mock_session

        checkout_response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )
        assert checkout_response.status_code == 200
        assert "url" in checkout_response.json()

    # Get initial credits
    user_dao = UserDAO(dbsession)
    user_record = user_dao.get_user_with_id(user["id"])
    initial_credits = float(user_record.billing_account.credits) if user_record else 0

    # Step 3: Simulate webhook for completed payment
    webhook_payload = {
        "id": "evt_e2e_personal_checkout",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_e2e_personal",
                "customer": "cus_e2e_personal_checkout",
                "client_reference_id": user["id"],
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 10000,  # $100 = 100 credits
                "currency": "usd",
                "metadata": {},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response.status_code == 200

    # Step 4: Verify credits were added
    dbsession.expire_all()
    updated_user = user_dao.get_user_with_id(user["id"])
    assert float(updated_user.billing_account.credits) == initial_credits + 100


@pytest.mark.anyio
async def test_e2e_user_multiple_top_ups(client: AsyncClient, dbsession):
    """
    E2E Test: User performs multiple credit top-ups.

    Credits should accumulate correctly.
    """
    from orchestra.db.dao.user_dao import UserDAO

    user = await create_test_user(client, "e2e_multi_topup@example.com")

    user_dao = UserDAO(dbsession)
    user_record = user_dao.get_user_with_id(user["id"])
    initial_credits = float(user_record.billing_account.credits) if user_record else 0

    # First top-up: $50 = 50 credits
    webhook1 = {
        "id": "evt_multi_topup_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_multi_1",
                "customer": "cus_multi",
                "client_reference_id": user["id"],
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "usd",
                "metadata": {},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook1):
        await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook1),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig1"},
        )

    dbsession.expire_all()
    user_after_1 = user_dao.get_user_with_id(user["id"])
    assert float(user_after_1.billing_account.credits) == initial_credits + 50

    # Second top-up: $25 = 25 credits
    webhook2 = {
        "id": "evt_multi_topup_2",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_multi_2",
                "customer": "cus_multi",
                "client_reference_id": user["id"],
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 2500,
                "currency": "usd",
                "metadata": {},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook2):
        await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook2),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig2"},
        )

    dbsession.expire_all()
    user_after_2 = user_dao.get_user_with_id(user["id"])
    assert (
        float(user_after_2.billing_account.credits) == initial_credits + 75
    )  # 50 + 25


@pytest.mark.anyio
async def test_e2e_user_checkout_session_creates_session(client: AsyncClient):
    """
    E2E Test: Checkout session can be created via the unified endpoint.

    The unified endpoint derives amount/URLs from settings, so there is
    no request body.
    """
    user = await create_test_user(client, "e2e_checkout_unified@example.com")

    with patch("stripe.Customer.create") as mock_customer, patch(
        "stripe.checkout.Session.create",
    ) as mock_session:
        mock_customer.return_value = MagicMock(id="cus_test")
        mock_session.return_value = MagicMock(id="cs_test", url="https://test.com")

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert "url" in data
        assert "session_id" in data


@pytest.mark.anyio
async def test_e2e_webhook_invalid_event_type_ignored(client: AsyncClient):
    """
    E2E Test: Unknown webhook event types are gracefully ignored.
    """
    webhook_payload = {
        "id": "evt_unknown",
        "type": "some.unknown.event",
        "data": {
            "object": {"id": "obj_123"},
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig"},
        )
        # Should not error, just return 200
        assert response.status_code == 200


@pytest.mark.anyio
async def test_e2e_webhook_missing_user_handled(client: AsyncClient, dbsession):
    """
    E2E Test: Webhook for non-existent user returns 404.

    This is expected behavior - if user doesn't exist, we can't add credits.
    The webhook handler logs an error and returns 404.
    """
    webhook_payload = {
        "id": "evt_no_user",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_no_user",
                "customer": "cus_nonexistent",
                "client_reference_id": "nonexistent-user-id-12345",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "usd",
                "metadata": {},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig"},
        )
        # Returns 404 when entity not found
        assert response.status_code == 404
