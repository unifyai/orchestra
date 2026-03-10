"""
Live Stripe Sandbox Tests for General Billing.

These tests hit the REAL Stripe sandbox API - they are skipped if
STRIPE_SECRET_KEY is not configured.

These tests focus on general billing operations that span both users and
organizations, including:
- Invoice creation
- Payment intents
- Webhook signature verification
- Credit card operations
- Stripe API connectivity

Requirements:
    - STRIPE_SECRET_KEY env var set (sandbox key starting with sk_test_)
    - Network access to Stripe API

Run these tests:
    # Set env vars first
    export STRIPE_SECRET_KEY=sk_test_xxx

    # Run the tests
    pytest orchestra/tests/test_billing/test_billing_live.py -v
"""

import os
import time

import pytest
from sqlalchemy.orm import Session

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


# ============================================================================
# Stripe API Connectivity Tests
# ============================================================================


async def test_live_stripe_api_connectivity():
    """
    LIVE TEST: Verify Stripe API is reachable and key is valid.

    This is a basic smoke test to ensure Stripe connectivity works.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Simple API call to verify connectivity
    balance = stripe.Balance.retrieve()

    assert balance is not None
    assert hasattr(balance, "available")
    assert hasattr(balance, "pending")


async def test_live_stripe_test_mode_verification():
    """
    LIVE TEST: Verify we're running in Stripe test mode.

    This ensures we don't accidentally hit production.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Test mode keys start with sk_test_
    assert STRIPE_SECRET_KEY.startswith("sk_test_"), "Must use test mode API key"

    # Create a test customer to verify test mode
    customer = stripe.Customer.create(
        email="test_mode_check@example.com",
        metadata={"test": "true"},
    )

    # Test mode customer IDs still start with cus_
    assert customer.id.startswith("cus_")

    # Verify it's in test mode by checking livemode flag
    assert customer.livemode is False

    # Clean up
    stripe.Customer.delete(customer.id)


# ============================================================================
# Invoice Tests
# ============================================================================


async def test_live_stripe_create_invoice_item(dbsession: Session):
    """
    LIVE TEST: Create an invoice item on a Stripe customer.

    This tests the core billing flow where we add usage charges.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create a customer
    customer = stripe.Customer.create(
        email=f"invoice_item_test_{os.urandom(4).hex()}@example.com",
        metadata={"test": "invoice_item"},
    )

    try:
        # Create an invoice item
        invoice_item = stripe.InvoiceItem.create(
            customer=customer.id,
            amount=1000,  # $10.00
            currency="usd",
            description="Test credit usage",
        )

        assert invoice_item.id.startswith("ii_")
        assert invoice_item.amount == 1000
        assert invoice_item.customer == customer.id

    finally:
        # Clean up
        stripe.Customer.delete(customer.id)


async def test_live_stripe_create_and_finalize_invoice(dbsession: Session):
    """
    LIVE TEST: Create and finalize an invoice.

    This tests the monthly invoicing flow where we aggregate charges.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create a customer
    customer = stripe.Customer.create(
        email=f"invoice_test_{os.urandom(4).hex()}@example.com",
        metadata={"test": "invoice"},
    )

    try:
        # Create an invoice item first (required for invoice)
        stripe.InvoiceItem.create(
            customer=customer.id,
            amount=2500,  # $25.00
            currency="usd",
            description="Monthly usage charges",
        )

        # Create and finalize invoice
        invoice = stripe.Invoice.create(
            customer=customer.id,
            auto_advance=False,  # Don't auto-collect
            collection_method="send_invoice",
            days_until_due=30,
        )

        assert invoice.id.startswith("in_")
        assert invoice.status == "draft"
        assert invoice.customer == customer.id

        # Finalize the invoice
        finalized = stripe.Invoice.finalize_invoice(invoice.id)
        # Status could be "open" (awaiting payment) or "paid" (auto-collected)
        assert finalized.status in [
            "open",
            "paid",
        ], f"Unexpected status: {finalized.status}"

        # Void it if still open (clean up)
        if finalized.status == "open":
            stripe.Invoice.void_invoice(invoice.id)

    finally:
        stripe.Customer.delete(customer.id)


# ============================================================================
# Webhook Signature Tests
# ============================================================================


async def test_live_stripe_webhook_signature_validation():
    """
    LIVE TEST: Verify webhook signature validation works.

    This tests our ability to validate Stripe webhook signatures.
    Note: This doesn't require STRIPE_WEBHOOK_SECRET for the test itself,
    it just verifies the signature logic works.
    """
    import hashlib
    import hmac

    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Test payload
    payload = '{"id": "evt_test", "type": "test.event"}'
    test_secret = "whsec_test_secret"

    # Generate valid signature
    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.{payload}"
    signature = hmac.new(
        test_secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    sig_header = f"t={timestamp},v1={signature}"

    # This should succeed
    event = stripe.Webhook.construct_event(
        payload=payload,
        sig_header=sig_header,
        secret=test_secret,
    )

    assert event["id"] == "evt_test"
    assert event["type"] == "test.event"


async def test_live_stripe_webhook_invalid_signature_rejected():
    """
    LIVE TEST: Verify invalid webhook signatures are rejected.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    payload = '{"id": "evt_test", "type": "test.event"}'
    test_secret = "whsec_test_secret"

    # Invalid signature
    sig_header = "t=123456,v1=invalid_signature"

    with pytest.raises(stripe.SignatureVerificationError):
        stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=test_secret,
        )


# ============================================================================
# Payment Method Tests
# ============================================================================


async def test_live_stripe_create_test_payment_method():
    """
    LIVE TEST: Attach a test payment method to a customer.

    Uses Stripe's test payment method token (pm_card_visa) instead of
    raw card numbers, which require special account permissions.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create a customer
    customer = stripe.Customer.create(
        email=f"payment_method_test_{os.urandom(4).hex()}@example.com",
    )

    try:
        # Use Stripe's pre-created test payment method token
        # pm_card_visa is automatically available in test mode
        payment_method = stripe.PaymentMethod.attach(
            "pm_card_visa",
            customer=customer.id,
        )

        assert payment_method.id.startswith("pm_")
        assert payment_method.type == "card"
        assert payment_method.card.last4 == "4242"
        assert payment_method.customer == customer.id

    finally:
        stripe.Customer.delete(customer.id)


# ============================================================================
# Customer Billing Configuration Tests
# ============================================================================


async def test_live_stripe_customer_default_payment_method():
    """
    LIVE TEST: Set and verify default payment method for a customer.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        email=f"default_pm_test_{os.urandom(4).hex()}@example.com",
    )

    try:
        # Attach a test payment method using Stripe's test token
        pm = stripe.PaymentMethod.attach(
            "pm_card_visa",
            customer=customer.id,
        )

        # Set as default
        stripe.Customer.modify(
            customer.id,
            invoice_settings={"default_payment_method": pm.id},
        )

        # Verify
        updated = stripe.Customer.retrieve(customer.id)
        assert updated.invoice_settings.default_payment_method == pm.id

    finally:
        stripe.Customer.delete(customer.id)


async def test_live_stripe_customer_with_billing_address():
    """
    LIVE TEST: Create customer with full billing address.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        email=f"billing_addr_test_{os.urandom(4).hex()}@example.com",
        name="Test Company Inc",
        address={
            "line1": "123 Test Street",
            "line2": "Suite 100",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94102",
            "country": "US",
        },
    )

    try:
        assert customer.address is not None
        assert customer.address.line1 == "123 Test Street"
        assert customer.address.city == "San Francisco"
        assert customer.address.country == "US"
        assert customer.name == "Test Company Inc"

    finally:
        stripe.Customer.delete(customer.id)


# ============================================================================
# Tax ID Tests
# ============================================================================


async def test_live_stripe_customer_tax_id():
    """
    LIVE TEST: Add and retrieve tax ID for a customer.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        email=f"tax_id_test_{os.urandom(4).hex()}@example.com",
    )

    try:
        # Add a tax ID (US EIN format)
        # Note: Stripe validates tax ID formats
        tax_id = stripe.Customer.create_tax_id(
            customer.id,
            type="us_ein",
            value="12-3456789",
        )

        assert tax_id.id.startswith("txi_")
        assert tax_id.type == "us_ein"
        # Stripe may mask or format the value
        assert "3456789" in tax_id.value or "12-3456789" in tax_id.value

        # List tax IDs
        tax_ids = stripe.Customer.list_tax_ids(customer.id)
        assert len(tax_ids.data) >= 1

        # Delete tax ID
        stripe.Customer.delete_tax_id(customer.id, tax_id.id)

        # Verify deleted
        tax_ids_after = stripe.Customer.list_tax_ids(customer.id)
        assert len(tax_ids_after.data) == 0

    finally:
        stripe.Customer.delete(customer.id)


# ============================================================================
# Checkout Session Tests
# ============================================================================


async def test_live_stripe_checkout_session_creation():
    """
    LIVE TEST: Create a checkout session directly via Stripe API.

    This tests the raw Stripe API call that our endpoints use.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        email=f"checkout_test_{os.urandom(4).hex()}@example.com",
    )

    try:
        session = stripe.checkout.Session.create(
            customer=customer.id,
            mode="payment",
            success_url="https://example.com/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://example.com/cancel",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": 5000,  # $50.00
                        "product_data": {
                            "name": "50 Credits",
                            "description": "Orchestra platform credits",
                        },
                    },
                    "quantity": 1,
                },
            ],
            metadata={
                "credits": "50",
                "test": "true",
            },
        )

        assert session.id.startswith("cs_test_")
        assert session.url.startswith("https://checkout.stripe.com/")
        assert session.payment_status == "unpaid"
        assert session.customer == customer.id
        assert session.metadata.credits == "50"

    finally:
        stripe.Customer.delete(customer.id)


async def test_live_stripe_checkout_session_with_custom_amounts():
    """
    LIVE TEST: Test checkout sessions with various credit amounts.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        email=f"amounts_test_{os.urandom(4).hex()}@example.com",
    )

    test_amounts = [5, 25, 100, 500]

    try:
        for amount in test_amounts:
            session = stripe.checkout.Session.create(
                customer=customer.id,
                mode="payment",
                success_url="https://example.com/success",
                cancel_url="https://example.com/cancel",
                line_items=[
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": amount * 100,  # Convert to cents
                            "product_data": {
                                "name": f"{amount} Credits",
                            },
                        },
                        "quantity": 1,
                    },
                ],
            )

            # Retrieve and verify
            retrieved = stripe.checkout.Session.retrieve(
                session.id,
                expand=["line_items"],
            )

            line_item = retrieved.line_items.data[0]
            assert line_item.amount_total == amount * 100

    finally:
        stripe.Customer.delete(customer.id)


# ============================================================================
# Subscription Tests (for future auto-recharge implementation)
# ============================================================================


async def test_live_stripe_create_price():
    """
    LIVE TEST: Create a price object for recurring billing.

    This tests price creation which could be used for subscription-based
    auto-recharge in the future.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create a product first
    product = stripe.Product.create(
        name=f"Test Credits Pack {os.urandom(4).hex()}",
        description="Test product for live billing tests",
    )

    try:
        # Create a price
        price = stripe.Price.create(
            product=product.id,
            unit_amount=2500,  # $25.00
            currency="usd",
        )

        assert price.id.startswith("price_")
        assert price.unit_amount == 2500
        assert price.active is True

        # Archive the price
        stripe.Price.modify(price.id, active=False)

    finally:
        # Archive the product
        stripe.Product.modify(product.id, active=False)
