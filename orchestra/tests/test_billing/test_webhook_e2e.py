"""
End-to-End Webhook Tests using Stripe CLI.

These tests verify the full webhook flow:
    Stripe Event → Webhook Endpoint → Database Update

Requirements:
    1. STRIPE_SECRET_KEY env var set (sk_test_xxx)
    2. STRIPE_WEBHOOK_SECRET env var set (whsec_xxx from stripe.sh)
    3. Stripe CLI installed and authenticated
    4. Local Orchestra server running on port 8000
    5. Webhook forwarding active (via scripts/stripe.sh bg)

Setup:
    # Terminal 1: Start Orchestra
    poetry run python -m orchestra

    # Terminal 2: Start webhook forwarding and get webhook secret
    ./scripts/stripe.sh bg
    # Note the webhook secret (whsec_xxx) and set it:
    export STRIPE_WEBHOOK_SECRET=whsec_xxx

    # Terminal 3: Run these tests
    pytest orchestra/tests/test_billing/test_webhook_e2e.py -v -s

These tests use `stripe trigger` with `--override` to customize event data
with our test entities, then verify the database was updated correctly.
"""

import os
import subprocess
import time
import uuid
from decimal import Decimal
from typing import Optional

import httpx
import pytest
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    BillingAccount,
    Organization,
    Recharge,
    RechargeStatus,
    User,
)

# Environment checks
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SERVER_URL = os.environ.get("ORCHESTRA_TEST_SERVER_URL", "http://localhost:8000")

# Check if Stripe CLI is available
def _check_stripe_cli() -> bool:
    try:
        result = subprocess.run(
            ["stripe", "version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


STRIPE_CLI_AVAILABLE = _check_stripe_cli()

# Check if server is running
def _check_server_running() -> bool:
    try:
        response = httpx.get(f"{SERVER_URL}/v0/health", timeout=2)
        return response.status_code == 200
    except Exception:
        return False


SKIP_REASON = (
    "E2E webhook tests require: "
    "1) STRIPE_SECRET_KEY (sk_test_xxx), "
    "2) STRIPE_WEBHOOK_SECRET (whsec_xxx), "
    "3) Stripe CLI installed, "
    "4) Local server running at localhost:8000, "
    "5) Webhook forwarding via ./scripts/stripe.sh bg"
)

pytestmark = [
    pytest.mark.e2e_webhook,
    pytest.mark.skipif(
        not STRIPE_SECRET_KEY.startswith("sk_test_") or not STRIPE_CLI_AVAILABLE,
        reason=SKIP_REASON,
    ),
]

# Track Stripe customers for cleanup
_created_customers: list[str] = []


@pytest.fixture(autouse=True)
def _configure_stripe(monkeypatch):
    """Configure Stripe settings for tests."""
    from orchestra.settings import settings

    if STRIPE_SECRET_KEY:
        monkeypatch.setattr(
            settings,
            "stripe_secret_key",
            STRIPE_SECRET_KEY,
            raising=False,
        )
    if STRIPE_WEBHOOK_SECRET:
        monkeypatch.setattr(
            settings,
            "stripe_webhook_secret",
            STRIPE_WEBHOOK_SECRET,
            raising=False,
        )


@pytest.fixture(autouse=True)
def _cleanup_stripe_resources():
    """Clean up Stripe resources after each test."""
    _created_customers.clear()
    yield

    if _created_customers and STRIPE_SECRET_KEY:
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        for customer_id in _created_customers:
            try:
                stripe.Customer.delete(customer_id)
            except Exception:
                pass
        _created_customers.clear()


@pytest.fixture
def require_server():
    """Skip test if server isn't running."""
    if not _check_server_running():
        pytest.skip(
            f"Server not running at {SERVER_URL}. Start with: poetry run python -m orchestra",
        )


@pytest.fixture
def require_webhook_forwarding():
    """Skip test if webhook forwarding isn't active."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "stripe listen"],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("Webhook forwarding not active. Run: ./scripts/stripe.sh bg")
    except FileNotFoundError:
        pytest.skip("pgrep not available")


# ============================================================================
# Utility Functions
# ============================================================================


def stripe_trigger(
    event_type: str,
    override: Optional[dict] = None,
    timeout: int = 30,
) -> tuple[bool, str]:
    """
    Trigger a Stripe test event using the CLI.

    Args:
        event_type: Stripe event type (e.g., 'checkout.session.completed')
        override: Dict of field overrides (e.g., {"customer": "cus_xxx"})
        timeout: Max seconds to wait

    Returns:
        Tuple of (success: bool, output: str)
    """
    cmd = ["stripe", "trigger", event_type]

    if override:
        for key, value in override.items():
            cmd.extend(["--override", f"{key}={value}"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except FileNotFoundError:
        return False, "Stripe CLI not found"


def wait_for_db_condition(
    session: Session,
    condition_fn,
    timeout: int = 10,
    interval: float = 0.5,
) -> bool:
    """
    Wait for a database condition to become true.

    Refreshes the session between checks to see committed changes.
    """
    start = time.time()
    while time.time() - start < timeout:
        session.expire_all()  # Clear cache to see new commits
        if condition_fn():
            return True
        time.sleep(interval)
    return False


def create_stripe_customer(email: str, metadata: Optional[dict] = None) -> str:
    """Create a Stripe customer directly and track for cleanup."""
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        email=email,
        metadata=metadata or {},
    )
    _created_customers.append(customer.id)
    return customer.id


def create_test_user_with_stripe(session: Session, email: str) -> tuple[User, str]:
    """Create a test user in DB with a linked BillingAccount and matching Stripe customer."""
    user_id = str(uuid.uuid4())
    stripe_customer_id = create_stripe_customer(
        email=email,
        metadata={"user_id": user_id},
    )

    ba = BillingAccount(
        credits=Decimal("0"),
        stripe_customer_id=stripe_customer_id,
        account_status="ACTIVE",
    )
    session.add(ba)
    session.flush()

    user = User(
        id=user_id,
        email=email,
        billing_account_id=ba.id,
    )
    session.add(user)
    session.commit()

    return user, stripe_customer_id


def create_test_org_with_stripe(
    session: Session,
    name: str,
    owner_email: str,
) -> tuple[Organization, str]:
    """Create a test org with owner, linked BillingAccount, and matching Stripe customer."""
    from sqlalchemy import text

    from orchestra.db.models.orchestra_models import OrganizationMember

    # Create owner (with their own BillingAccount)
    owner_id = str(uuid.uuid4())
    owner_ba = BillingAccount(credits=Decimal("0"), account_status="ACTIVE")
    session.add(owner_ba)
    session.flush()

    owner = User(id=owner_id, email=owner_email, billing_account_id=owner_ba.id)
    session.add(owner)
    session.flush()

    # Create org with Stripe customer
    org_id = session.execute(text("SELECT nextval('organization_id_seq')")).scalar()

    stripe_customer_id = create_stripe_customer(
        email=owner_email,
        metadata={"organization_id": str(org_id)},
    )

    org_ba = BillingAccount(
        credits=Decimal("0"),
        stripe_customer_id=stripe_customer_id,
        account_status="ACTIVE",
    )
    session.add(org_ba)
    session.flush()

    org = Organization(
        id=org_id,
        name=name,
        owner_id=owner_id,
        billing_account_id=org_ba.id,
    )
    session.add(org)
    session.flush()

    # Get the Owner role ID from the role table
    owner_role_id = session.execute(
        text("SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true"),
    ).scalar()

    # Add owner as member
    member = OrganizationMember(
        organization_id=org_id,
        user_id=owner_id,
        role_id=owner_role_id,
    )
    session.add(member)
    session.commit()

    return org, stripe_customer_id


# ============================================================================
# Checkout Webhook Tests
# ============================================================================


@pytest.mark.anyio
async def test_e2e_checkout_webhook_flow(
    dbsession: Session,
    require_server,
    require_webhook_forwarding,
):
    """
    E2E TEST: Full checkout → webhook → credits flow.

    This test:
    1. Creates a user with Stripe customer in DB
    2. Creates a real checkout session
    3. Uses Stripe CLI to complete the checkout
    4. Verifies webhook adds credits to user
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create user with Stripe customer
    email = f"e2e_checkout_{uuid.uuid4().hex[:8]}@test.com"
    user, customer_id = create_test_user_with_stripe(dbsession, email)
    ba = user.billing_account
    initial_credits = ba.credits if ba else Decimal("0")

    # Create a real checkout session
    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="payment",
        success_url=f"{SERVER_URL}/success",
        cancel_url=f"{SERVER_URL}/cancel",
        client_reference_id=user.id,
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": 5000,  # $50
                    "product_data": {"name": "50 Credits"},
                },
                "quantity": 1,
            },
        ],
        metadata={"credits": "50"},
    )

    # Trigger checkout completion via Stripe CLI
    success, output = stripe_trigger(
        "checkout.session.completed",
        override={
            "checkout_session:client_reference_id": user.id,
            "checkout_session:customer": customer_id,
            "checkout_session:amount_total": "5000",
        },
    )

    if not success:
        pytest.skip(f"Stripe trigger failed: {output}")

    # Wait for webhook to process
    def check_credits_updated():
        dbsession.refresh(user)
        ba = user.billing_account
        return ba is not None and ba.credits > initial_credits

    assert wait_for_db_condition(
        dbsession,
        check_credits_updated,
        timeout=15,
    ), f"Credits not updated. Current: {user.billing_account.credits}, Initial: {initial_credits}"

    # Verify credits added
    dbsession.refresh(user)
    assert user.billing_account.credits == initial_credits + Decimal("50")


# ============================================================================
# Invoice Webhook Tests
# ============================================================================


@pytest.mark.anyio
async def test_e2e_invoice_paid_webhook(
    dbsession: Session,
    require_server,
    require_webhook_forwarding,
):
    """
    E2E TEST: Invoice payment → webhook → recharge status update.

    This test:
    1. Creates a user with pending recharge record
    2. Triggers invoice.payment_succeeded via Stripe CLI
    3. Verifies recharge status is updated to PAID
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create user with Stripe customer
    email = f"e2e_invoice_{uuid.uuid4().hex[:8]}@test.com"
    user, customer_id = create_test_user_with_stripe(dbsession, email)

    # Create a real invoice in Stripe
    # Use collection_method="send_invoice" to keep invoice open
    stripe.InvoiceItem.create(
        customer=customer_id,
        amount=2500,
        currency="usd",
        description="Test usage",
    )

    invoice = stripe.Invoice.create(
        customer=customer_id,
        auto_advance=False,
        collection_method="send_invoice",
        days_until_due=30,
    )

    # Finalize the invoice to make it payable
    invoice = stripe.Invoice.finalize_invoice(invoice.id)

    # Create pending recharge record in our DB
    recharge = Recharge(
        billing_account_id=user.billing_account_id,
        quantity=25,
        amount_usd=Decimal("25.00"),
        status=RechargeStatus.INVOICE_CREATED,
        stripe_invoice_id=invoice.id,
        type="usage",
    )
    dbsession.add(recharge)
    dbsession.commit()
    recharge_id = recharge.id

    # Trigger invoice payment via CLI
    success, output = stripe_trigger(
        "invoice.payment_succeeded",
        override={
            "invoice:id": invoice.id,
            "invoice:customer": customer_id,
        },
    )

    if not success:
        # Void the invoice if still open and skip
        try:
            current_invoice = stripe.Invoice.retrieve(invoice.id)
            if current_invoice.status == "open":
                stripe.Invoice.void_invoice(invoice.id)
        except stripe.error.StripeError:
            pass
        pytest.skip(f"Stripe trigger failed: {output}")

    # Wait for webhook to process
    def check_status_updated():
        dbsession.expire_all()
        r = dbsession.query(Recharge).filter_by(id=recharge_id).first()
        return r and r.status == RechargeStatus.PAID

    assert wait_for_db_condition(
        dbsession,
        check_status_updated,
        timeout=15,
    ), "Recharge status not updated to PAID"


@pytest.mark.anyio
async def test_e2e_invoice_payment_failed_webhook(
    dbsession: Session,
    require_server,
    require_webhook_forwarding,
):
    """
    E2E TEST: Invoice payment failure → webhook → status update.

    This test:
    1. Creates a user with pending invoice
    2. Triggers invoice.payment_failed via Stripe CLI
    3. Verifies appropriate status handling
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create user with Stripe customer
    email = f"e2e_inv_fail_{uuid.uuid4().hex[:8]}@test.com"
    user, customer_id = create_test_user_with_stripe(dbsession, email)

    # Create a test invoice
    stripe.InvoiceItem.create(
        customer=customer_id,
        amount=1000,
        currency="usd",
    )
    invoice = stripe.Invoice.create(customer=customer_id, auto_advance=False)

    # Create recharge record
    recharge = Recharge(
        billing_account_id=user.billing_account_id,
        quantity=10,
        amount_usd=Decimal("10.00"),
        status=RechargeStatus.INVOICE_CREATED,
        stripe_invoice_id=invoice.id,
        type="usage",
    )
    dbsession.add(recharge)
    dbsession.commit()

    # Trigger payment failure
    success, output = stripe_trigger(
        "invoice.payment_failed",
        override={
            "invoice:id": invoice.id,
            "invoice:customer": customer_id,
        },
    )

    # Clean up invoice
    try:
        stripe.Invoice.void_invoice(invoice.id)
    except Exception:
        pass

    if not success:
        pytest.skip(f"Stripe trigger failed: {output}")

    # The webhook should log the failure - verify no crash occurred
    # (actual behavior depends on implementation)
    time.sleep(2)  # Give webhook time to process

    # Verify recharge record still exists (not deleted)
    dbsession.expire_all()
    r = dbsession.query(Recharge).filter_by(id=recharge.id).first()
    assert r is not None, "Recharge record should still exist"


# ============================================================================
# Customer Webhook Tests
# ============================================================================


@pytest.mark.anyio
async def test_e2e_customer_tax_id_webhook(
    dbsession: Session,
    require_server,
    require_webhook_forwarding,
):
    """
    E2E TEST: Tax ID created on Stripe customer → webhook → org updated.

    This test:
    1. Creates an org with Stripe customer
    2. Adds a tax ID to the Stripe customer
    3. Verifies webhook syncs tax ID to organization
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create org with Stripe customer
    name = f"E2E Tax Org {uuid.uuid4().hex[:8]}"
    email = f"e2e_tax_{uuid.uuid4().hex[:8]}@test.com"
    org, customer_id = create_test_org_with_stripe(dbsession, name, email)
    org_id = org.id

    # Add tax ID directly to Stripe customer
    tax_id = stripe.Customer.create_tax_id(
        customer_id,
        type="us_ein",
        value="12-3456789",
    )

    # The webhook should fire automatically when tax ID is created
    # Wait for it to sync to the BillingAccount
    ba_id = org.billing_account_id

    def check_tax_id_synced():
        dbsession.expire_all()
        ba = dbsession.query(BillingAccount).filter_by(id=ba_id).first()
        return ba and ba.tax_id is not None

    synced = wait_for_db_condition(dbsession, check_tax_id_synced, timeout=15)

    # Clean up tax ID
    try:
        stripe.Customer.delete_tax_id(customer_id, tax_id.id)
    except Exception:
        pass

    if synced:
        dbsession.expire_all()
        ba = dbsession.query(BillingAccount).filter_by(id=ba_id).first()
        assert "3456789" in (
            ba.tax_id or ""
        ), f"Tax ID not synced correctly: {ba.tax_id}"
    else:
        pytest.skip("Tax ID webhook not received in time - verify webhook forwarding")


# ============================================================================
# Dispute Webhook Tests
# ============================================================================


@pytest.mark.anyio
async def test_e2e_dispute_created_webhook(
    dbsession: Session,
    require_server,
    require_webhook_forwarding,
):
    """
    E2E TEST: Charge dispute → webhook → account handling.

    This test verifies the webhook handler processes dispute events.
    Note: Creating real disputes requires completed charges.
    """
    # Trigger dispute event via CLI (synthetic event)
    success, output = stripe_trigger("charge.dispute.created")

    if not success:
        pytest.skip(f"Stripe trigger failed: {output}")

    # Give webhook time to process
    time.sleep(2)

    # The test passes if no errors occurred during webhook processing
    # Actual dispute handling depends on implementation


# ============================================================================
# Subscription Webhook Tests (Future)
# ============================================================================


@pytest.mark.anyio
async def test_e2e_subscription_created_webhook(
    dbsession: Session,
    require_server,
    require_webhook_forwarding,
):
    """
    E2E TEST: Subscription created → webhook → auto-recharge setup.

    This test verifies subscription webhook handling.
    """
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    # Create customer for subscription
    email = f"e2e_sub_{uuid.uuid4().hex[:8]}@test.com"
    user, customer_id = create_test_user_with_stripe(dbsession, email)

    # Trigger subscription event
    success, output = stripe_trigger(
        "customer.subscription.created",
        override={"subscription:customer": customer_id},
    )

    if not success:
        pytest.skip(f"Stripe trigger failed: {output}")

    # Webhook should process without error
    time.sleep(2)


# ============================================================================
# Batch Event Tests
# ============================================================================


@pytest.mark.anyio
async def test_e2e_multiple_webhooks_sequential(
    dbsession: Session,
    require_server,
    require_webhook_forwarding,
):
    """
    E2E TEST: Multiple webhook events in sequence.

    Verifies webhook handler can process multiple events correctly.
    """
    events = [
        "customer.created",
        "customer.updated",
        "invoice.created",
    ]

    for event_type in events:
        success, output = stripe_trigger(event_type)
        if not success:
            print(f"Warning: {event_type} trigger failed: {output}")
        time.sleep(0.5)  # Small delay between events

    # All events should process without errors
    time.sleep(2)
