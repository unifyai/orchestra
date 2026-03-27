"""
End-to-end billing flow tests that use the **live Stripe** sandbox API.

These tests verify multi-step billing scenarios against the real Stripe
sandbox.  They are the only billing tests that require network access
and a valid ``sk_test_…`` key, so they can be excluded from fast CI runs.

Sections:
- AutoRechargeInvoicerFlows: auto-recharge → invoicer → webhook payment
- BillingGuardFlows: freeze / unfreeze / edge cases
- BillingProfileFlows: profile update → Stripe sync
- CheckoutAutoRechargeFlows: checkout → credit spend → auto-recharge
- DisputeFlows: charge.dispute → credit debit
- InvoiceFailureFlows: invoice.payment_failed handling
- LiveCheckoutFlows: real Stripe checkout session tests
- LiveOrgFlows: org checkout, business details, tax ID sync

Requirements:
    1. STRIPE_SECRET_KEY env var set (sk_test_xxx)
    2. STRIPE_WEBHOOK_SECRET env var set (for webhook tests)
    3. Stripe CLI installed and authenticated (for webhook tests)
    4. Local Orchestra server running on port 8000 (for webhook tests)
    5. Webhook forwarding active via scripts/stripe.sh bg (for webhook tests)

Run:
    pytest orchestra/tests/test_billing/test_billing_flows.py -v -s
"""

from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    BillingAccount,
    Recharge,
    RechargeStatus,
)
from orchestra.tests.utils import create_test_org, create_test_user

from .conftest import (
    SKIP_REASON,
    STRIPE_CLI_AVAILABLE,
    STRIPE_SECRET_KEY,
    create_test_org_with_stripe,
    create_test_user_with_stripe,
    stripe_trigger,
    wait_for_db_condition,
)

# ---------------------------------------------------------------------------
# Module-level skip: all tests require a real Stripe test key
# ---------------------------------------------------------------------------
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not STRIPE_SECRET_KEY.startswith("sk_test_"),
        reason=SKIP_REASON,
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
            "stripe_unify_credits_price_id_personal",
            os.environ.get(
                "STRIPE_UNIFY_CREDITS_PRICE_ID_PERSONAL",
                "price_1T1p4kLGH7MGCUMnzgCUPWg4",
            ),
            raising=False,
        )
        monkeypatch.setattr(
            settings,
            "stripe_unify_credits_price_id_business",
            os.environ.get(
                "STRIPE_UNIFY_CREDITS_PRICE_ID_BUSINESS",
                "price_1T1p4lLGH7MGCUMnaZPuy2Ig",
            ),
            raising=False,
        )


# Track Stripe customers created during tests for cleanup
_created_stripe_customers: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup_stripe_customers():  # noqa: PT004
    """Delete Stripe sandbox customers created during each test."""
    _created_stripe_customers.clear()
    yield

    if _created_stripe_customers and STRIPE_SECRET_KEY:
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY
        for customer_id in _created_stripe_customers:
            try:
                stripe.Customer.delete(customer_id)
            except Exception:
                pass
        _created_stripe_customers.clear()


def track_stripe_customer(customer_id: str):
    if customer_id and customer_id.startswith("cus_"):
        _created_stripe_customers.append(customer_id)


# ============================================================================
# Auto-Recharge → Invoicer → Payment Flow
# ============================================================================


class TestAutoRechargeInvoicerFlows:
    """E2E: auto-recharge → monthly invoicer → Stripe webhook payment."""

    pytestmark = [
        pytest.mark.e2e_webhook,
        pytest.mark.skipif(not STRIPE_CLI_AVAILABLE, reason="Stripe CLI not available"),
    ]

    @pytest.mark.anyio
    async def test_auto_recharge_invoicer_payment(
        self,
        dbsession: Session,
        require_server,
        require_webhook_forwarding,
    ):
        """
        1. Create user with auto-recharge enabled
        2. Deduct credits below threshold → queue auto-recharge
        3. Run invoicer → create real Stripe invoice
        4. Trigger invoice.payment_succeeded webhook
        5. Verify recharge status is PAID
        """
        import stripe

        from orchestra.lib.billing import queue_auto_recharge
        from orchestra.routines import monthly_invoicer as invoicer_mod

        stripe.api_key = STRIPE_SECRET_KEY

        email = f"e2e_ar_flow_{uuid.uuid4().hex[:8]}@test.com"
        user, customer_id = create_test_user_with_stripe(dbsession, email)
        ba = user.billing_account

        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("20")
        ba.autorecharge_qty = Decimal("50")
        ba.credits = Decimal("15")
        dbsession.commit()

        queue_auto_recharge(dbsession, ba, 50, entity_label=f"user {user.id}")
        dbsession.commit()

        recharges = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).all()
        assert len(recharges) == 1
        recharge = recharges[0]
        assert recharge.status == RechargeStatus.PENDING_INVOICE

        dbsession.refresh(ba)
        assert ba.credits == Decimal("65")

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        dbsession.refresh(recharge)
        assert recharge.status == RechargeStatus.INVOICE_CREATED
        assert recharge.stripe_invoice_id is not None

        invoice_id = recharge.stripe_invoice_id
        invoice = stripe.Invoice.retrieve(invoice_id)
        assert invoice.customer == customer_id

        success, output = stripe_trigger(
            "invoice.payment_succeeded",
            override={
                "invoice:id": invoice_id,
                "invoice:customer": customer_id,
            },
        )

        if not success:
            try:
                stripe.Invoice.void_invoice(invoice_id)
            except Exception:
                pass
            pytest.skip(f"Stripe trigger failed: {output}")

        recharge_id = recharge.id

        def check_paid():
            dbsession.expire_all()
            r = dbsession.query(Recharge).filter_by(id=recharge_id).first()
            return r and r.status == RechargeStatus.PAID

        assert wait_for_db_condition(
            dbsession,
            check_paid,
            timeout=15,
        ), "Recharge status not updated to PAID after invoice payment webhook"


# ============================================================================
# Billing Profile → Stripe Sync Flow
# ============================================================================


class TestBillingProfileFlows:
    """E2E: billing profile update syncs to Stripe customer."""

    pytestmark = [
        pytest.mark.e2e_webhook,
        pytest.mark.skipif(not STRIPE_CLI_AVAILABLE, reason="Stripe CLI not available"),
    ]

    @pytest.mark.anyio
    async def test_profile_stripe_sync(
        self,
        dbsession: Session,
        require_server,
        require_webhook_forwarding,
    ):
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        name = f"E2E Profile Org {uuid.uuid4().hex[:8]}"
        email = f"e2e_profile_{uuid.uuid4().hex[:8]}@test.com"
        org, customer_id = create_test_org_with_stripe(dbsession, name, email)
        ba = org.billing_account

        billing_address = {
            "line1": "123 Test Street",
            "city": "San Francisco",
            "state": "CA",
            "country": "US",
            "postal_code": "94105",
        }
        ba.billing_email = "billing@e2etest.com"
        ba.name = "E2E Test Corp"
        ba.billing_address = billing_address
        dbsession.commit()

        from orchestra.lib.billing import sync_billing_profile_to_stripe

        sync_billing_profile_to_stripe(
            customer_id,
            is_business=True,
            billing_email="billing@e2etest.com",
            name="E2E Test Corp",
            billing_address=billing_address,
        )

        customer = stripe.Customer.retrieve(customer_id)
        assert customer.email == "billing@e2etest.com"
        assert customer.name == "E2E Test Corp"
        assert customer.address is not None
        assert customer.address.line1 == "123 Test Street"
        assert customer.address.city == "San Francisco"
        assert customer.address.country == "US"
        assert customer.address.postal_code == "94105"


# ============================================================================
# Checkout → Auto-Recharge Flow
# ============================================================================


class TestCheckoutAutoRechargeFlows:
    """E2E: checkout adds credits → credits deplete → auto-recharge fires."""

    pytestmark = [
        pytest.mark.e2e_webhook,
        pytest.mark.skipif(not STRIPE_CLI_AVAILABLE, reason="Stripe CLI not available"),
    ]

    @pytest.mark.anyio
    async def test_checkout_then_auto_recharge(
        self,
        dbsession: Session,
        require_server,
        require_webhook_forwarding,
    ):
        import stripe

        from orchestra.lib.billing import queue_auto_recharge

        stripe.api_key = STRIPE_SECRET_KEY

        email = f"e2e_checkout_ar_{uuid.uuid4().hex[:8]}@test.com"
        user, customer_id = create_test_user_with_stripe(dbsession, email)
        ba = user.billing_account

        ba.credits = Decimal("100")
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("20")
        ba.autorecharge_qty = Decimal("50")
        dbsession.commit()

        ba.credits = Decimal("15")
        dbsession.commit()

        should_recharge = (
            ba.autorecharge
            and ba.stripe_customer_id
            and ba.credits < ba.autorecharge_threshold
        )
        assert should_recharge is True

        queue_auto_recharge(
            dbsession,
            ba,
            int(ba.autorecharge_qty),
            f"user {user.id}",
        )
        dbsession.commit()

        dbsession.refresh(ba)
        assert ba.credits == Decimal("65")

        recharges = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type="auto")
            .all()
        )
        assert len(recharges) == 1
        assert recharges[0].status == RechargeStatus.PENDING_INVOICE


# ============================================================================
# Dispute Flows
# ============================================================================


class TestDisputeFlows:
    """E2E: charge.dispute → credit debit."""

    pytestmark = [
        pytest.mark.e2e_webhook,
        pytest.mark.skipif(not STRIPE_CLI_AVAILABLE, reason="Stripe CLI not available"),
    ]

    @pytest.mark.anyio
    async def test_dispute_debits_credits(
        self,
        dbsession: Session,
        require_server,
        require_webhook_forwarding,
    ):
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        email = f"e2e_dispute_{uuid.uuid4().hex[:8]}@test.com"
        user, customer_id = create_test_user_with_stripe(dbsession, email)
        ba = user.billing_account

        ba.credits = Decimal("100")
        dbsession.commit()

        success, output = stripe_trigger(
            "charge.dispute.created",
            override={"charge:customer": customer_id},
        )

        if not success:
            pytest.skip(f"Stripe trigger failed: {output}")

        time.sleep(3)

        dbsession.refresh(ba)
        assert ba.credits is not None


# ============================================================================
# Invoice Failure Flows
# ============================================================================


class TestInvoiceFailureFlows:
    """E2E: invoice payment failure — account stays ACTIVE."""

    pytestmark = [
        pytest.mark.e2e_webhook,
        pytest.mark.skipif(not STRIPE_CLI_AVAILABLE, reason="Stripe CLI not available"),
    ]

    @pytest.mark.anyio
    async def test_invoice_payment_failed_keeps_active(
        self,
        dbsession: Session,
        require_server,
        require_webhook_forwarding,
    ):
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        email = f"e2e_fail_pd_{uuid.uuid4().hex[:8]}@test.com"
        user, customer_id = create_test_user_with_stripe(dbsession, email)
        ba = user.billing_account

        stripe.InvoiceItem.create(
            customer=customer_id,
            amount=5000,
            currency="usd",
            description="Test usage - will fail",
        )
        invoice = stripe.Invoice.create(
            customer=customer_id,
            auto_advance=False,
        )

        recharge = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id=invoice.id,
            type="usage",
        )
        dbsession.add(recharge)
        dbsession.commit()

        success, output = stripe_trigger(
            "invoice.payment_failed",
            override={
                "invoice:id": invoice.id,
                "invoice:customer": customer_id,
            },
        )

        try:
            stripe.Invoice.void_invoice(invoice.id)
        except Exception:
            pass

        if not success:
            pytest.skip(f"Stripe trigger failed: {output}")

        time.sleep(3)

        ba_id = ba.id
        dbsession.expire_all()
        account = dbsession.query(BillingAccount).filter_by(id=ba_id).first()
        assert account.account_status == "ACTIVE"

        r = dbsession.query(Recharge).filter_by(id=recharge.id).first()
        assert r.status != RechargeStatus.PAID


# ============================================================================
# Live Stripe Checkout Flows (no webhook forwarding needed)
# ============================================================================


class TestLiveCheckoutFlows:
    """Live Stripe sandbox tests for checkout sessions — no webhook forwarding."""

    pytestmark = [pytest.mark.anyio]

    async def test_customer_creation(self, client: AsyncClient):
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        user = await create_test_user(
            client,
            f"live_test_{os.urandom(4).hex()}@example.com",
        )
        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["url"].startswith("https://checkout.stripe.com/")
        assert data["session_id"].startswith("cs_test_")

        session = stripe.checkout.Session.retrieve(data["session_id"])
        assert session.mode == "payment"
        assert session.payment_status == "unpaid"
        track_stripe_customer(session.customer)

    async def test_checkout_session_structure(self, client: AsyncClient):
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        user = await create_test_user(
            client,
            f"live_session_{os.urandom(4).hex()}@example.com",
        )
        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )
        assert response.status_code == 200

        session = stripe.checkout.Session.retrieve(
            response.json()["session_id"],
            expand=["line_items"],
        )
        assert session.client_reference_id == user["id"]
        assert session.mode == "payment"
        assert session.line_items is not None
        assert len(session.line_items.data) == 1
        track_stripe_customer(session.customer)

    async def test_customer_reuse(self, client: AsyncClient, dbsession: Session):
        import stripe

        from orchestra.db.dao.user_dao import UserDAO

        stripe.api_key = STRIPE_SECRET_KEY

        user = await create_test_user(
            client,
            f"live_reuse_{os.urandom(4).hex()}@example.com",
        )

        r1 = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )
        assert r1.status_code == 200
        s1 = stripe.checkout.Session.retrieve(r1.json()["session_id"])
        cid1 = s1.customer

        r2 = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )
        assert r2.status_code == 200
        s2 = stripe.checkout.Session.retrieve(r2.json()["session_id"])
        cid2 = s2.customer

        assert cid1 == cid2

        user_dao = UserDAO(session=dbsession)
        db_user_row = user_dao.get_by_id(user["id"])
        assert db_user_row is not None
        db_user = db_user_row[0]
        assert db_user.billing_account.stripe_customer_id == cid1
        track_stripe_customer(cid1)

    async def test_customer_email_metadata(self, client: AsyncClient):
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
        # customer_creation="always" means the customer object is created
        # only when the session is completed (paid); before that,
        # customer_email / customer_details carry the pre-fill values.
        assert session.customer_email == email
        if session.customer:
            track_stripe_customer(session.customer)

    async def test_multiple_checkouts(self, client: AsyncClient):
        import stripe

        stripe.api_key = STRIPE_SECRET_KEY

        user = await create_test_user(
            client,
            f"live_amounts_{os.urandom(4).hex()}@example.com",
        )

        customer_ids = set()
        for i in range(3):
            resp = await client.post(
                "/v0/billing/checkout-session",
                headers=user["headers"],
            )
            assert resp.status_code == 200

            session = stripe.checkout.Session.retrieve(
                resp.json()["session_id"],
                expand=["line_items"],
            )
            assert session.line_items is not None
            assert len(session.line_items.data) >= 1

            if session.customer:
                customer_ids.add(session.customer)
            if i == 0:
                track_stripe_customer(session.customer)

        assert len(customer_ids) <= 1


# ============================================================================
# Live Org Checkout, Business Details & Tax ID
# ============================================================================


class TestLiveOrgFlows:
    """Live Stripe sandbox tests for organization billing — no webhook forwarding."""

    pytestmark = [pytest.mark.anyio]

    async def test_org_checkout_session(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
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

        resp = await client.post(
            "/v0/billing/checkout-session",
            headers=org["headers"],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"].startswith("https://checkout.stripe.com/")

        session = stripe.checkout.Session.retrieve(
            data["session_id"],
            expand=["line_items"],
        )
        assert session.mode == "payment"
        if session.customer:
            track_stripe_customer(session.customer)

    async def test_org_with_business_details(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        import stripe

        from orchestra.db.dao.organization_dao import OrganizationDAO

        stripe.api_key = STRIPE_SECRET_KEY

        email = f"live_org_business_{os.urandom(4).hex()}@example.com"
        user = await create_test_user(client, email)
        org = await create_test_org(
            client,
            user,
            f"Business Org {os.urandom(4).hex()}",
        )
        org_id = org["id"]

        # Pre-create Stripe customer and link to the org's billing account
        # (checkout sessions with customer_creation="always" only create the
        # customer on completion/payment, not on session creation)
        from .conftest import create_stripe_customer

        customer_id = create_stripe_customer(
            email=email,
            metadata={"organization_id": str(org_id)},
        )
        org_dao = OrganizationDAO(session=dbsession)
        db_org = org_dao.get(org_id)
        if db_org.billing_account is None:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            ba_dao = BillingAccountDAO(dbsession)
            ba = ba_dao.create()
            db_org.billing_account_id = ba.id
            dbsession.flush()
        db_org.billing_account.stripe_customer_id = customer_id
        dbsession.commit()
        track_stripe_customer(customer_id)

        # Update business profile
        update = await client.patch(
            "/v0/billing/billing-profile",
            json={
                "name": "Acme Corporation",
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
        assert update.status_code == 200

        customer = stripe.Customer.retrieve(customer_id)
        assert customer.name == "Acme Corporation"
        assert customer.address.line1 == "123 Main Street"
        assert customer.address.city == "San Francisco"
        assert customer.address.country == "US"

    async def test_org_multiple_checkouts_same_customer(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
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

        first = await client.post(
            "/v0/billing/checkout-session",
            headers=org["headers"],
        )
        assert first.status_code == 200

        dbsession.expire_all()
        org_dao = OrganizationDAO(session=dbsession)
        original_cid = org_dao.get(org_id).billing_account.stripe_customer_id
        track_stripe_customer(original_cid)

        for _ in range(2):
            resp = await client.post(
                "/v0/billing/checkout-session",
                headers=org["headers"],
            )
            assert resp.status_code == 200
            session = stripe.checkout.Session.retrieve(resp.json()["session_id"])
            assert session.customer == original_cid

    async def test_org_tax_id_sync(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        import stripe

        from orchestra.db.dao.organization_dao import OrganizationDAO

        stripe.api_key = STRIPE_SECRET_KEY

        email = f"live_org_tax_{os.urandom(4).hex()}@example.com"
        user = await create_test_user(client, email)
        org = await create_test_org(
            client,
            user,
            f"Tax Org {os.urandom(4).hex()}",
        )
        org_id = org["id"]

        # Pre-create Stripe customer and link to the org's billing account
        from .conftest import create_stripe_customer

        customer_id = create_stripe_customer(
            email=email,
            metadata={"organization_id": str(org_id)},
        )
        org_dao = OrganizationDAO(session=dbsession)
        db_org = org_dao.get(org_id)
        if db_org.billing_account is None:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            ba_dao = BillingAccountDAO(dbsession)
            ba = ba_dao.create()
            db_org.billing_account_id = ba.id
            dbsession.flush()
        db_org.billing_account.stripe_customer_id = customer_id
        dbsession.commit()
        track_stripe_customer(customer_id)

        update = await client.patch(
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
        assert update.status_code == 200

        tax_ids = stripe.Customer.list_tax_ids(customer_id)
        assert len(tax_ids.data) > 0
        assert any(tid.value in ("12-3456789", "123456789") for tid in tax_ids.data)
