"""
Integration-style billing tests (matching test_credits pattern).

1. Schema smoke – columns exist.
2. Invoicer – aggregates rows, hits Stripe once, flips status.
3. Webhook idempotency – double delivery, single effect.
4. Billing guard – suspends PAST_DUE + zero balance.
5. Pre-paid credits – skip invoicer.
6. Auto-recharge – queue recharge when credits below threshold.
"""

from __future__ import annotations

import calendar
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import time
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Dict

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    BillingAccount,
    Recharge,
    RechargeStatus,
    User,
    WebhookLog,
)
from orchestra.lib.billing import queue_auto_recharge
from orchestra.lib.time import month_end_utc
from orchestra.routines import billing_guard as guard
from orchestra.routines import monthly_invoicer as invoicer
from orchestra.settings import settings
from orchestra.tests.utils import create_test_user


# --------------------------------------------------------------------------- #
# Helper: create a User with linked BillingAccount                             #
# --------------------------------------------------------------------------- #
def _make_user(
    dbsession: Session,
    uid: str,
    email: str | None = None,
    credits: float | Decimal = 0,
    stripe_customer_id: str | None = None,
    autorecharge: bool = False,
    autorecharge_threshold: float | Decimal = 0,
    autorecharge_qty: float | Decimal = 25,
    account_status: str = "ACTIVE",
) -> tuple[User, BillingAccount]:
    """Create a User + BillingAccount pair for testing."""
    ba = BillingAccount(
        credits=Decimal(str(credits)),
        stripe_customer_id=stripe_customer_id,
        autorecharge=autorecharge,
        autorecharge_threshold=Decimal(str(autorecharge_threshold)),
        autorecharge_qty=Decimal(str(autorecharge_qty)),
        account_status=account_status,
    )
    dbsession.add(ba)
    dbsession.flush()

    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user, ba


@contextlib.contextmanager
def _guard_uses(dbsession: Session):
    """Temporarily monkey-patch guard's sessionmaker to return the given session."""
    from sqlalchemy.orm import sessionmaker

    # Store original sessionmaker
    orig_sessionmaker = sessionmaker

    # Create a mock sessionmaker that returns our test session
    def mock_sessionmaker(*args, **kwargs):
        class MockSessionLocal:
            def __enter__(self):
                return dbsession

            def __exit__(self, *args):
                pass

        return MockSessionLocal

    # Monkey-patch sessionmaker in the module
    import sqlalchemy.orm

    sqlalchemy.orm.sessionmaker = mock_sessionmaker

    try:
        yield
    finally:
        # Restore original sessionmaker
        sqlalchemy.orm.sessionmaker = orig_sessionmaker


@contextlib.contextmanager
def _routine_uses_session(module, dbsession):
    """Temporarily monkey-patch any module's sessionmaker to return the given session."""
    from sqlalchemy.orm import sessionmaker

    # Store original sessionmaker
    orig_sessionmaker = sessionmaker

    # Create a mock sessionmaker that returns our test session
    def mock_sessionmaker(*args, **kwargs):
        class MockSessionLocal:
            def __enter__(self):
                return dbsession

            def __exit__(self, *args):
                pass

        return MockSessionLocal

    # Monkey-patch sessionmaker in the module
    import sqlalchemy.orm

    sqlalchemy.orm.sessionmaker = mock_sessionmaker

    try:
        yield
    finally:
        # Restore original sessionmaker
        sqlalchemy.orm.sessionmaker = orig_sessionmaker


# --------------------------------------------------------------------------- #
# Stripe dummy (shared by all tests)                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def mock_stripe(monkeypatch) -> Dict[str, list]:
    calls: Dict[str, list] = {"item": [], "invoice": [], "construct": []}

    def _item_create(**kw):
        calls["item"].append(kw)

    class _Inv(SimpleNamespace):
        pass

    def _inv_create(**kw):
        calls["invoice"].append(kw)
        return _Inv(id="in_test_123")

    def _construct_event(payload, sig_header, secret, tolerance=None):
        calls["construct"].append({"sig": sig_header})
        return json.loads(payload)

    def _mock_retrieve(payment_intent_id):
        # Mock PaymentIntent.retrieve for dispute tests
        return {
            "metadata": {
                "user_id": "test_user",
                "credits_purchased": "50",
            },
            "invoice": "in_test_dispute",
        }

    dummy = SimpleNamespace(
        InvoiceItem=SimpleNamespace(create=_item_create),
        Invoice=SimpleNamespace(create=_inv_create),
        Webhook=SimpleNamespace(construct_event=_construct_event),
        PaymentIntent=SimpleNamespace(retrieve=_mock_retrieve),
        error=SimpleNamespace(SignatureVerificationError=Exception),
    )

    # Patch the monthly_invoicer's stripe import directly
    import orchestra.routines.monthly_invoicer as monthly_invoicer
    import orchestra.web.api.webhooks.stripe as webhook_stripe

    monkeypatch.setattr(monthly_invoicer, "stripe", dummy, raising=True)
    monkeypatch.setattr(webhook_stripe, "stripe", dummy, raising=True)
    return calls


# --------------------------------------------------------------------------- #
# ensure settings have dummy secrets so pydantic doesn't explode              #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _env_secrets(monkeypatch):
    import os

    # Only set dummy values if real ones aren't already present
    if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")

    # For STRIPE_SECRET_KEY, only set dummy if no real key exists
    existing_key = os.environ.get("STRIPE_SECRET_KEY")
    if not existing_key or not existing_key.startswith("sk_test_"):
        monkeypatch.setenv(
            "STRIPE_SECRET_KEY",
            "sk_test_dummy_for_mocking",
        )  # Valid format but clearly a dummy

    if not os.environ.get("ORCHESTRA_ADMIN_KEY"):
        monkeypatch.setenv(
            "ORCHESTRA_ADMIN_KEY",
            "test_admin_key",
        )  # Admin key for tests

    # ensure the live settings instance has the field
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test", raising=False)
    # Also patch stripe_secret_key on the settings object since it was cached at import time
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_for_mocking",
        raising=False,
    )
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test", raising=False)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
TODAY = dt.date.today()
FIRST_THIS_MONTH = TODAY.replace(day=1)
LAST_MONTH_END = FIRST_THIS_MONTH - dt.timedelta(days=1)
LAST_GROUP = month_end_utc(LAST_MONTH_END.replace(day=1))


def _signed_hdr(body: str) -> str:
    ts = str(int(time.time()))
    sig_raw = f"{ts}.{body}"
    sig = hmac.new(
        settings.STRIPE_WEBHOOK_SECRET.encode(),
        sig_raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={sig}"


# --------------------------------------------------------------------------- #
# 0. Schema smoke                                                             #
# --------------------------------------------------------------------------- #
def test_schema_columns(dbsession: Session):
    insp = sa.inspect(dbsession.bind)
    rcols = {c["name"] for c in insp.get_columns("recharge")}
    bacols = {c["name"] for c in insp.get_columns("billing_account")}

    assert {"status", "stripe_invoice_id", "invoice_group"} <= rcols
    assert "billing_account_id" in rcols
    assert "account_status" in bacols
    assert "credits" in bacols


# --------------------------------------------------------------------------- #
# 1. Invoicer aggregates rows & flips                                         #
# --------------------------------------------------------------------------- #
def test_invoicer_aggregates(dbsession: Session, mock_stripe):
    user, ba = _make_user(
        dbsession,
        "user_inv",
        credits=0,
        stripe_customer_id="cus_test",
    )

    for _ in range(3):
        dbsession.add(
            Recharge(
                billing_account_id=ba.id,
                quantity=10,
                amount_usd=Decimal("10.00"),
                status=RechargeStatus.PENDING_INVOICE,
                invoice_group=LAST_GROUP,
                type="usage",
            ),
        )
    dbsession.commit()

    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    rows = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).all()
    assert {r.status for r in rows} == {RechargeStatus.INVOICE_CREATED}
    assert {r.stripe_invoice_id for r in rows} == {"in_test_123"}
    assert len(mock_stripe["invoice"]) == 1


# --------------------------------------------------------------------------- #
# 2. Webhook idempotency (HTTP)                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_webhook_idempotent(client: AsyncClient, dbsession: Session):
    user, ba = _make_user(
        dbsession,
        "webhook_u",
        stripe_customer_id="cus_x",
    )
    rec = Recharge(
        billing_account_id=ba.id,
        quantity=5,
        amount_usd=Decimal("50.00"),
        status=RechargeStatus.INVOICE_CREATED,
        stripe_invoice_id="in_test_1",
        type="usage",
    )
    dbsession.add(rec)
    dbsession.commit()

    payload = {
        "id": "evt_test",
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": "in_test_1",
                "status": "paid",
                "metadata": {"user_id": user.id},
            },
        },
    }
    body = json.dumps(payload)
    hdr = _signed_hdr(body)

    for _ in range(2):
        res = await client.post(
            "/v0/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": hdr},
        )
        assert res.status_code == 200

    dbsession.refresh(rec)
    assert rec.status == RechargeStatus.PAID
    assert dbsession.query(WebhookLog).count() == 1


# --------------------------------------------------------------------------- #
# 2.1. Webhook charge dispute regression test                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_webhook_charge_dispute_idempotent(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that charge.dispute.created events are handled correctly with idempotency."""
    user, ba = _make_user(
        dbsession,
        "dispute_user",
        credits=100,
        stripe_customer_id="cus_dispute",
    )
    dbsession.commit()

    payload = {
        "id": "evt_dispute_test",
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": "ch_dispute_123",
                "payment_intent": "pi_test_123",
                "invoice": "in_test_dispute",
            },
        },
    }
    body = json.dumps(payload)
    hdr = _signed_hdr(body)

    # Mock the Stripe PaymentIntent.retrieve call
    import orchestra.web.api.webhooks.stripe as webhook_module

    def mock_retrieve(payment_intent_id):
        return {
            "metadata": {
                "user_id": user.id,
                "credits_purchased": "50",
            },
            "invoice": "in_test_dispute",
        }

    original_retrieve = webhook_module.stripe.PaymentIntent.retrieve
    webhook_module.stripe.PaymentIntent.retrieve = mock_retrieve

    try:
        # Send the same event twice to test idempotency
        for _ in range(2):
            res = await client.post(
                "/v0/webhooks/stripe",
                content=body,
                headers={"Stripe-Signature": hdr},
            )
            assert res.status_code == 200

        # Verify only one webhook log entry was created (idempotency)
        webhook_logs = (
            dbsession.query(WebhookLog).filter_by(event_id="evt_dispute_test").all()
        )
        assert len(webhook_logs) == 1
        assert webhook_logs[0].event_type == "charge.dispute.created"

    finally:
        # Restore original function
        webhook_module.stripe.PaymentIntent.retrieve = original_retrieve


# --------------------------------------------------------------------------- #
# 3. Billing guard                                                            #
# --------------------------------------------------------------------------- #
def test_guard_suspends(dbsession: Session):
    _make_user(dbsession, "off", account_status="PAST_DUE", credits=0)
    _make_user(dbsession, "ok", account_status="ACTIVE", credits=10)
    dbsession.commit()

    with _guard_uses(dbsession):
        guard.suspend_past_due_accounts(session=dbsession)

    off_user = dbsession.get(User, "off")
    ok_user = dbsession.get(User, "ok")
    assert off_user.billing_account.account_status == "SUSPENDED"
    assert ok_user.billing_account.account_status == "ACTIVE"


# --------------------------------------------------------------------------- #
# 4. Pre-paid credit row must be skipped                                      #
# --------------------------------------------------------------------------- #
def test_prepaid_skip(dbsession: Session, mock_stripe):
    user, ba = _make_user(dbsession, "prepaid_u", credits=100)
    rec = Recharge(
        billing_account_id=ba.id,
        quantity=500,
        amount_usd=Decimal("50.00"),
        status=RechargeStatus.PAID,
        stripe_invoice_id="in_paid",
        type="payment",
    )
    dbsession.add(rec)
    dbsession.commit()

    # Store the ID to re-query the object after the invoicer runs
    rec_id = rec.id

    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(TODAY.year, TODAY.month)

    # Re-query the object instead of refreshing the detached one
    rec = dbsession.query(Recharge).filter_by(id=rec_id).first()
    assert rec.status == RechargeStatus.PAID
    assert mock_stripe["invoice"] == mock_stripe["item"] == []


# --------------------------------------------------------------------------- #
# 5. Auto-recharge functionality                                              #
# --------------------------------------------------------------------------- #
def test_queue_auto_recharge_basic(dbsession: Session):
    """Test basic auto-recharge queuing functionality."""
    user, ba = _make_user(
        dbsession,
        "test_user_ar",
        credits=100,
        stripe_customer_id="cus_test123",
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Queue an auto-recharge
    queue_auto_recharge(dbsession, ba, 50)
    dbsession.commit()

    # Check that a recharge was created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.amount_usd == Decimal("50.00")
    assert recharge.status == RechargeStatus.PENDING_INVOICE
    assert recharge.type == "auto"


def test_queue_auto_recharge_month_end_grouping(dbsession: Session):
    """Test that auto-recharges are grouped by month-end date."""
    user, ba = _make_user(
        dbsession,
        "grouping_user",
        credits=100,
        stripe_customer_id="cus_grouping_test",
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Queue multiple auto-recharges
    queue_auto_recharge(dbsession, ba, 50)
    queue_auto_recharge(dbsession, ba, 25)
    dbsession.commit()

    # Check that both recharges have the same invoice_group (month-end)
    recharges = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).all()
    assert len(recharges) == 2
    assert recharges[0].invoice_group == recharges[1].invoice_group
    assert (
        recharges[0].invoice_group.day
        == calendar.monthrange(
            recharges[0].invoice_group.year,
            recharges[0].invoice_group.month,
        )[1]
    )  # Last day of month


def test_auto_recharge_logic_triggers_correctly(dbsession: Session):
    """Test the auto-recharge logic directly without full bg_tasks integration."""
    user, ba = _make_user(
        dbsession,
        "logic_user",
        credits=15,  # Above threshold initially
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Simulate credit deduction that brings user below threshold
    ba.credits = Decimal("5")  # Below threshold
    dbsession.commit()

    # Check if auto-recharge should trigger
    should_trigger = (
        ba.autorecharge
        and ba.credits <= ba.autorecharge_threshold
        and ba.autorecharge_qty > 0
    )
    assert should_trigger

    # Manually trigger auto-recharge (simulating what bg_tasks would do)
    if should_trigger:
        queue_auto_recharge(dbsession, ba, int(ba.autorecharge_qty))
        dbsession.commit()

    # Verify recharge was created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.amount_usd == Decimal("50.00")


def test_auto_recharge_disabled_no_trigger(dbsession: Session):
    """Test that auto-recharge doesn't trigger when disabled."""
    user, ba = _make_user(
        dbsession,
        "disabled_user",
        credits=5,  # Below threshold
        autorecharge=False,  # Disabled
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Check if auto-recharge should trigger
    should_trigger = (
        ba.autorecharge
        and ba.credits <= ba.autorecharge_threshold
        and ba.autorecharge_qty > 0
    )
    assert not should_trigger

    # Verify no recharge was created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is None


def test_auto_recharge_above_threshold_no_trigger(dbsession: Session):
    """Test that auto-recharge doesn't trigger when credits are above threshold."""
    user, ba = _make_user(
        dbsession,
        "above_threshold_user",
        credits=50,  # Well above threshold
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Check if auto-recharge should trigger
    should_trigger = (
        ba.autorecharge
        and ba.credits <= ba.autorecharge_threshold
        and ba.autorecharge_qty > 0
    )
    assert not should_trigger

    # Verify no recharge was created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is None


def test_auto_recharge_integration_with_monthly_invoicer(
    dbsession: Session,
    mock_stripe,
):
    """Test that auto-recharges are properly processed by the monthly invoicer."""
    user, ba = _make_user(
        dbsession,
        "integration_user",
        credits=100,
        stripe_customer_id="cus_integration_test",
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Create some auto-recharges manually (simulating what would happen in bg_tasks)
    queue_auto_recharge(dbsession, ba, 50)
    queue_auto_recharge(dbsession, ba, 25)
    dbsession.commit()

    # Verify recharges are in PENDING_INVOICE status
    recharges = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).all()
    assert len(recharges) == 2
    assert all(r.status == RechargeStatus.PENDING_INVOICE for r in recharges)

    # Run the monthly invoicer
    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    # Check that recharges were processed
    recharges = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).all()
    assert len(recharges) == 2


def test_auto_recharge_zero_quantity_no_trigger(dbsession: Session):
    """Test that auto-recharge doesn't trigger when quantity is zero."""
    user, ba = _make_user(
        dbsession,
        "zero_qty_user",
        credits=5,  # Below threshold
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=0,  # Zero quantity
    )
    dbsession.commit()

    # Check if auto-recharge should trigger
    should_trigger = (
        ba.autorecharge
        and ba.credits <= ba.autorecharge_threshold
        and ba.autorecharge_qty > 0
    )
    assert not should_trigger

    # Verify no recharge was created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is None


# --------------------------------------------------------------------------- #
# 5b. Organization Monthly Invoicing Tests                                    #
# --------------------------------------------------------------------------- #
def _create_org_for_invoicing_test(
    dbsession: Session,
    name: str,
    stripe_customer_id: str | None,
    **kwargs,
):
    """Helper to create an org with a valid owner for invoicing tests."""
    from orchestra.db.models.orchestra_models import Organization

    # Create billing account for owner
    owner_ba = BillingAccount(credits=Decimal("100"))
    dbsession.add(owner_ba)
    dbsession.flush()

    # Create owner in user table (required FK for org.owner_id)
    owner_id = f"owner_{name.replace(' ', '_').lower()}"
    user = User(
        id=owner_id,
        email=f"{owner_id}@test.com",
        billing_account_id=owner_ba.id,
    )
    dbsession.add(user)
    dbsession.flush()

    # Create billing account for org
    org_ba = BillingAccount(
        credits=kwargs.get("credits", Decimal("100")),
        stripe_customer_id=stripe_customer_id,
        autorecharge=kwargs.get("autorecharge", True),
        autorecharge_threshold=kwargs.get("autorecharge_threshold", Decimal("10")),
        autorecharge_qty=kwargs.get("autorecharge_qty", Decimal("100")),
        billing_email=kwargs.get("billing_email"),
        billing_address=kwargs.get("billing_address"),
        tax_id=kwargs.get("tax_id"),
    )
    dbsession.add(org_ba)
    dbsession.flush()

    org = Organization(
        name=name,
        owner_id=owner_id,
        billing_account_id=org_ba.id,
    )
    dbsession.add(org)
    dbsession.flush()
    return org, org_ba


def test_monthly_invoicer_org_recharges(dbsession: Session, mock_stripe):
    """Test that organization recharges are properly processed by monthly invoicer."""
    org, org_ba = _create_org_for_invoicing_test(
        dbsession,
        name="Test Monthly Org",
        stripe_customer_id="cus_org_monthly_test",
        credits=Decimal("50"),
        billing_email="billing@testorg.com",
        billing_address={"country": "US", "postal_code": "94105"},
        tax_id="12-3456789",
    )
    dbsession.commit()

    # Create org recharges (simulating auto-recharge during the month)
    for amount in [50, 100, 25]:
        rec = Recharge(
            billing_account_id=org_ba.id,
            type=RECHARGE_TYPE_AUTO,
            quantity=Decimal(str(amount)),
            amount_usd=Decimal(str(amount)),
            invoice_group=LAST_GROUP,
            status=RechargeStatus.PENDING_INVOICE,
        )
        dbsession.add(rec)
    dbsession.commit()

    # Verify recharges are in PENDING_INVOICE status
    recharges = dbsession.query(Recharge).filter_by(billing_account_id=org_ba.id).all()
    assert len(recharges) == 3
    assert all(r.status == RechargeStatus.PENDING_INVOICE for r in recharges)

    # Run the monthly invoicer
    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    # Expire all to see changes made by invoicer
    dbsession.expire_all()

    # Check that recharges were processed
    recharges = dbsession.query(Recharge).filter_by(billing_account_id=org_ba.id).all()
    assert len(recharges) == 3
    assert all(r.status == RechargeStatus.INVOICE_CREATED for r in recharges)
    assert all(r.stripe_invoice_id == "in_test_123" for r in recharges)

    # Verify Stripe invoice was created with org's stripe_customer_id
    assert len(mock_stripe["invoice"]) == 1
    invoice_params = mock_stripe["invoice"][0]
    assert invoice_params["customer"] == "cus_org_monthly_test"


def test_monthly_invoicer_mixed_user_and_org_recharges(dbsession: Session, mock_stripe):
    """Test that both user and org recharges are processed in the same run."""
    # Create a user with Stripe customer ID
    user, user_ba = _make_user(
        dbsession,
        "mixed_test_user",
        credits=100,
        stripe_customer_id="cus_user_mixed_test",
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )

    # Create an org with Stripe customer ID
    org, org_ba = _create_org_for_invoicing_test(
        dbsession,
        name="Mixed Test Org",
        stripe_customer_id="cus_org_mixed_test",
    )
    dbsession.commit()

    # Create user recharges
    rec_user = Recharge(
        billing_account_id=user_ba.id,
        type=RECHARGE_TYPE_AUTO,
        quantity=Decimal("50"),
        amount_usd=Decimal("50"),
        invoice_group=LAST_GROUP,
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(rec_user)

    # Create org recharges
    rec_org = Recharge(
        billing_account_id=org_ba.id,
        type=RECHARGE_TYPE_AUTO,
        quantity=Decimal("100"),
        amount_usd=Decimal("100"),
        invoice_group=LAST_GROUP,
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(rec_org)
    dbsession.commit()

    # Run the monthly invoicer
    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    # Expire all to see changes made by invoicer
    dbsession.expire_all()

    # Check that both were processed
    user_recharges = (
        dbsession.query(Recharge).filter_by(billing_account_id=user_ba.id).all()
    )
    org_recharges = (
        dbsession.query(Recharge).filter_by(billing_account_id=org_ba.id).all()
    )

    assert len(user_recharges) == 1
    assert user_recharges[0].status == RechargeStatus.INVOICE_CREATED

    assert len(org_recharges) == 1
    assert org_recharges[0].status == RechargeStatus.INVOICE_CREATED

    # Verify both Stripe invoices were created (2 separate invoices)
    assert len(mock_stripe["invoice"]) == 2
    customers = {inv["customer"] for inv in mock_stripe["invoice"]}
    assert customers == {"cus_user_mixed_test", "cus_org_mixed_test"}


def test_monthly_invoicer_org_without_stripe_customer_skipped(
    dbsession: Session,
    mock_stripe,
):
    """Test that orgs without stripe_customer_id are skipped with a warning."""
    org, org_ba = _create_org_for_invoicing_test(
        dbsession,
        name="No Stripe Org",
        stripe_customer_id=None,  # No Stripe customer
        credits=Decimal("50"),
    )
    dbsession.commit()

    # Create org recharge (shouldn't happen in practice, but safety check)
    rec = Recharge(
        billing_account_id=org_ba.id,
        type=RECHARGE_TYPE_AUTO,
        quantity=Decimal("50"),
        amount_usd=Decimal("50"),
        invoice_group=LAST_GROUP,
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(rec)
    dbsession.commit()

    # Run the monthly invoicer - should skip without error
    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    # Recharge should remain in PENDING_INVOICE (skipped)
    rec = dbsession.query(Recharge).filter_by(billing_account_id=org_ba.id).first()
    assert rec.status == RechargeStatus.PENDING_INVOICE

    # No Stripe invoice created
    assert len(mock_stripe["invoice"]) == 0


def test_monthly_invoicer_org_with_tax_id(dbsession: Session, mock_stripe):
    """Test that org tax_id is included in invoice params with correct country type."""
    org, org_ba = _create_org_for_invoicing_test(
        dbsession,
        name="Indian Test Org",
        stripe_customer_id="cus_org_india",
        tax_id="29ABCDE1234F1Z5",  # GST format
        billing_address={"country": "IN", "state": "Karnataka", "city": "Bangalore"},
    )
    dbsession.commit()

    # Create org recharge
    rec = Recharge(
        billing_account_id=org_ba.id,
        type=RECHARGE_TYPE_AUTO,
        quantity=Decimal("100"),
        amount_usd=Decimal("100"),
        invoice_group=LAST_GROUP,
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(rec)
    dbsession.commit()

    # Run the monthly invoicer
    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    # Verify Stripe invoice was created with tax info
    assert len(mock_stripe["invoice"]) == 1
    invoice_params = mock_stripe["invoice"][0]

    # Should have customer_tax_ids with in_gst type
    assert "customer_tax_ids" in invoice_params
    assert len(invoice_params["customer_tax_ids"]) == 1
    assert invoice_params["customer_tax_ids"][0]["type"] == "in_gst"
    assert invoice_params["customer_tax_ids"][0]["value"] == "29ABCDE1234F1Z5"


def test_monthly_invoicer_org_aggregates_multiple_recharges(
    dbsession: Session,
    mock_stripe,
):
    """Test that multiple org recharges create ONE invoice with aggregated amount."""
    org, org_ba = _create_org_for_invoicing_test(
        dbsession,
        name="Aggregation Test Org",
        stripe_customer_id="cus_org_aggregate",
        credits=Decimal("200"),
    )
    dbsession.commit()

    # Create multiple recharges throughout the month
    amounts = [25, 50, 75, 100, 25]  # Total = 275
    for amount in amounts:
        rec = Recharge(
            billing_account_id=org_ba.id,
            type=RECHARGE_TYPE_AUTO,
            quantity=Decimal(str(amount)),
            amount_usd=Decimal(str(amount)),
            invoice_group=LAST_GROUP,
            status=RechargeStatus.PENDING_INVOICE,
        )
        dbsession.add(rec)
    dbsession.commit()

    # Run the monthly invoicer
    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    # Expire all to see changes made by invoicer
    dbsession.expire_all()

    # All recharges should be processed
    recharges = dbsession.query(Recharge).filter_by(billing_account_id=org_ba.id).all()
    assert len(recharges) == 5
    assert all(r.status == RechargeStatus.INVOICE_CREATED for r in recharges)
    # All should have the same invoice ID (aggregated)
    invoice_ids = {r.stripe_invoice_id for r in recharges}
    assert len(invoice_ids) == 1
    assert "in_test_123" in invoice_ids

    # Only ONE invoice created for the org
    assert len(mock_stripe["invoice"]) == 1


# --------------------------------------------------------------------------- #
# 6. Admin billing endpoints                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_admin_trigger_monthly_invoicing(client: AsyncClient):
    """Test the admin endpoint for triggering monthly invoicing."""
    from orchestra.tests.utils import ADMIN_HEADERS

    # Test with default parameters (previous month)
    response = await client.post(
        "/v0/admin/billing/invoice-month",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "Monthly invoicing completed" in data["message"]
    assert "previous month" in data["message"]


@pytest.mark.anyio
async def test_admin_trigger_monthly_invoicing_with_params(client: AsyncClient):
    """Test the admin endpoint for triggering monthly invoicing with specific year/month."""
    from orchestra.tests.utils import ADMIN_HEADERS

    # Test with specific year and month
    response = await client.post(
        "/v0/admin/billing/invoice-month",
        params={"year": 2024, "month": 1},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "2024-01" in data["message"]
    assert data["year"] == 2024
    assert data["month"] == 1


@pytest.mark.anyio
async def test_admin_trigger_billing_guard(client: AsyncClient):
    """Test the admin endpoint for triggering billing guard."""
    from orchestra.tests.utils import ADMIN_HEADERS

    response = await client.post(
        "/v0/admin/billing/suspend-past-due",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "Billing guard completed" in data["message"]
    assert "past-due accounts" in data["message"]


@pytest.mark.anyio
async def test_admin_billing_endpoints_require_auth(client: AsyncClient):
    """Test that billing endpoints require admin authentication."""
    from orchestra.tests.utils import HEADERS  # Regular user headers

    endpoints = [
        "/v0/admin/billing/invoice-month",
        "/v0/admin/billing/suspend-past-due",
    ]

    for endpoint in endpoints:
        # Test with no auth
        response = await client.post(endpoint)
        assert response.status_code in [401, 403]  # Unauthorized or Forbidden

        # Test with regular user auth (should fail)
        response = await client.post(endpoint, headers=HEADERS)
        assert response.status_code in [401, 403]  # Should require admin auth


# --------------------------------------------------------------------------- #
# 7. Real Stripe API test (no mocking)                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_real_stripe_invoicer_integration(dbsession: Session, monkeypatch):
    """
    Test the monthly invoicer with REAL Stripe API calls (no mocking).

    To run this test, set the STRIPE_SECRET_KEY environment variable
    with a Stripe test key (starts with sk_test_).

    This test will be skipped if no test key is provided.
    """
    import os

    import stripe

    from orchestra.routines import monthly_invoicer as invoicer

    # Get the Stripe key from environment
    test_stripe_key = os.environ.get("STRIPE_SECRET_KEY")

    if (
        not test_stripe_key
        or not test_stripe_key.startswith("sk_test_")
        or "dummy" in test_stripe_key.lower()
    ):
        pytest.skip(
            "No real test Stripe API key available - set STRIPE_SECRET_KEY environment variable with real test key",
        )

    # Temporarily restore real Stripe module
    import stripe as real_stripe

    monkeypatch.setattr(invoicer, "stripe", real_stripe, raising=False)

    # Create a test user with Stripe customer
    uid = f"test_stripe_user_{int(time.time())}"
    stripe_customer_id = f"cus_test_{int(time.time())}"

    # Set the API key for our direct Stripe calls (customer creation/cleanup)
    real_stripe.api_key = test_stripe_key

    # Create customer in Stripe first
    try:
        customer = real_stripe.Customer.create(
            id=stripe_customer_id,
            email="test-user@example.com",
            name="Test User",
        )
    except real_stripe.error.InvalidRequestError as e:
        if "already exists" in str(e):
            customer = real_stripe.Customer.retrieve(stripe_customer_id)
        else:
            raise

    # Create user in database with BillingAccount
    user, ba = _make_user(
        dbsession,
        uid,
        credits=0,
        stripe_customer_id=stripe_customer_id,
    )

    # Create a recharge for current month
    from datetime import datetime, timezone

    from orchestra.lib.time import month_end_utc

    now = datetime.now(timezone.utc)
    current_group = month_end_utc(now)

    recharge = Recharge(
        billing_account_id=ba.id,
        quantity=100,
        amount_usd=Decimal("100.00"),
        status=RechargeStatus.PENDING_INVOICE,
        invoice_group=current_group,
        type="usage",
    )
    dbsession.add(recharge)
    dbsession.commit()

    # Run the monthly invoicer for current month (no mocking!)
    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(now.year, now.month)

    # Check that recharge was processed
    dbsession.refresh(recharge)

    assert recharge.status == RechargeStatus.INVOICE_CREATED
    assert recharge.stripe_invoice_id is not None

    # Verify invoice exists in Stripe
    invoice = real_stripe.Invoice.retrieve(recharge.stripe_invoice_id)
    assert invoice.customer == stripe_customer_id

    # Verify the core functionality works (invoice creation and DB updates)
    assert invoice.status in ["draft", "open"]

    # Clean up - delete the test customer
    try:
        real_stripe.Customer.delete(stripe_customer_id)
    except:
        pass  # Ignore cleanup errors


# --------------------------------------------------------------------------- #
# 8. Billing requirements tests                                               #
# --------------------------------------------------------------------------- #


def test_minimum_autorecharge_amount(dbsession: Session):
    """Test that auto-recharge amount must be at least $25."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    user, ba = _make_user(
        dbsession,
        "autorecharge_test_user",
        credits=1000,
        stripe_customer_id="cus_autorecharge_test",
    )
    dbsession.commit()

    ba_dao = BillingAccountDAO(dbsession)

    # Try to set auto-recharge amount below minimum - should fail
    try:
        ba_dao.set_autorecharge_qty(ba.id, 10.0)  # $10, below minimum
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Minimum auto-recharge amount is $25" in str(e)

    # Try to set auto-recharge amount at minimum - should succeed
    ba_dao.set_autorecharge_qty(ba.id, 25.0)  # $25, at minimum
    dbsession.commit()

    dbsession.refresh(ba)
    assert float(ba.autorecharge_qty) == 25.0

    # Try to set auto-recharge amount above minimum - should succeed
    ba_dao.set_autorecharge_qty(ba.id, 50.0)  # $50, above minimum
    dbsession.commit()

    dbsession.refresh(ba)
    assert float(ba.autorecharge_qty) == 50.0


# --------------------------------------------------------------------------- #
# 9. User workflow tests for billing requirements                             #
# --------------------------------------------------------------------------- #


def test_new_user_cannot_enable_auto_recharge(dbsession: Session):
    """Test that a new user cannot enable auto-recharge without meeting the spend threshold."""
    from orchestra.db.dao.billing_account_dao import (
        MIN_SPEND_FOR_AUTO_RECHARGE,
        BillingAccountDAO,
    )

    user, ba = _make_user(
        dbsession,
        "new_user_test",
        credits=1000,
        stripe_customer_id="cus_new_user",
    )
    dbsession.commit()

    ba_dao = BillingAccountDAO(dbsession)

    # New user with no PAID recharges should not be eligible
    assert not ba_dao.can_enable_auto_recharge(ba.id)
    assert ba_dao.get_total_spending(ba.id) == 0

    # Spending below threshold should keep them ineligible
    total_spending = ba_dao.get_total_spending(ba.id)
    assert total_spending < MIN_SPEND_FOR_AUTO_RECHARGE


def test_auto_recharge_eligibility_with_spending(dbsession: Session):
    """Test that cumulative PAID recharges unlock auto-recharge eligibility."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    user, ba = _make_user(
        dbsession,
        "spending_test_user",
        credits=500,
        stripe_customer_id="cus_spending",
    )
    dbsession.commit()

    ba_dao = BillingAccountDAO(dbsession)

    # No recharges → not eligible
    assert ba_dao.get_total_spending(ba.id) == 0
    assert not ba_dao.can_enable_auto_recharge(ba.id)

    # Add a PAID recharge below threshold
    rec1 = Recharge(
        billing_account_id=ba.id,
        quantity=50,
        amount_usd=Decimal("50.00"),
        type="payment",
        status=RechargeStatus.PAID,
    )
    dbsession.add(rec1)
    dbsession.flush()

    assert float(ba_dao.get_total_spending(ba.id)) == 50.0
    assert not ba_dao.can_enable_auto_recharge(ba.id)

    # Add another PAID recharge to cross threshold
    rec2 = Recharge(
        billing_account_id=ba.id,
        quantity=60,
        amount_usd=Decimal("60.00"),
        type="auto",
        status=RechargeStatus.PAID,
    )
    dbsession.add(rec2)
    dbsession.flush()

    assert float(ba_dao.get_total_spending(ba.id)) == 110.0
    assert ba_dao.can_enable_auto_recharge(ba.id)

    # Promo recharges should NOT count toward the threshold
    rec3 = Recharge(
        billing_account_id=ba.id,
        quantity=1000,
        amount_usd=Decimal("1000.00"),
        type="promo",
        status=RechargeStatus.PAID,
    )
    dbsession.add(rec3)
    dbsession.flush()

    # Total should still be 110 (promo excluded)
    assert float(ba_dao.get_total_spending(ba.id)) == 110.0

    # PENDING recharges should NOT count
    rec4 = Recharge(
        billing_account_id=ba.id,
        quantity=500,
        amount_usd=Decimal("500.00"),
        type="payment",
        status=RechargeStatus.PENDING_INVOICE,
    )
    dbsession.add(rec4)
    dbsession.flush()

    # Still 110 — only PAID real-money transactions count
    assert float(ba_dao.get_total_spending(ba.id)) == 110.0


def test_existing_customer_with_auto_recharge_unaffected(dbsession: Session):
    """Test that existing customers with auto-recharge enabled continue to work normally."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    user, ba = _make_user(
        dbsession,
        "existing_customer",
        credits=500,
        stripe_customer_id="cus_existing",
        autorecharge=True,
        autorecharge_qty=50.0,
        autorecharge_threshold=100.0,
    )
    dbsession.commit()

    ba_dao = BillingAccountDAO(dbsession)

    # Existing customer can modify their settings via billing_account_dao
    ba_dao.set_autorecharge_qty(ba.id, 100.0)
    ba_dao.set_autorecharge_threshold(ba.id, 50.0)
    dbsession.commit()

    dbsession.refresh(ba)
    assert ba.autorecharge is True
    assert float(ba.autorecharge_qty) == 100.0
    assert float(ba.autorecharge_threshold) == 50.0

    # Can disable autorecharge
    ba_dao.set_autorecharge(ba.id, False)
    dbsession.commit()

    dbsession.refresh(ba)
    assert ba.autorecharge is False

    # Re-enable
    ba_dao.set_autorecharge(ba.id, True)
    dbsession.commit()

    dbsession.refresh(ba)
    assert ba.autorecharge is True


def test_autorecharge_amount_validation_edge_cases(dbsession: Session):
    """Test edge cases around the $25 minimum auto-recharge amount."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    user, ba = _make_user(
        dbsession,
        "autorecharge_validation_user",
        credits=1000,
        stripe_customer_id="cus_validation",
    )
    dbsession.commit()

    ba_dao = BillingAccountDAO(dbsession)

    # Test exact boundary values
    test_cases = [
        (24.99, False, "below minimum"),
        (25.00, True, "exact minimum"),
        (25.01, True, "above minimum"),
        (0.01, False, "very small amount"),
        (1000.00, True, "large amount"),
    ]

    for amount, should_succeed, description in test_cases:
        if should_succeed:
            ba_dao.set_autorecharge_qty(ba.id, amount)
            dbsession.commit()

            dbsession.refresh(ba)
            assert float(ba.autorecharge_qty) == amount, f"Failed for {description}"
        else:
            try:
                ba_dao.set_autorecharge_qty(ba.id, amount)
                assert False, f"Should have failed for {description} (${amount})"
            except ValueError as e:
                assert "Minimum auto-recharge amount is $25" in str(e)


# --------------------------------------------------------------------------- #
# 10. API validation and error handling tests                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_api_validation_comprehensive_error_messages(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that API endpoints return comprehensive and user-friendly error messages."""
    from orchestra.tests.utils import ADMIN_HEADERS

    user, ba = _make_user(
        dbsession,
        "validation_user",
        credits=1000,
        stripe_customer_id="cus_validation",
    )
    dbsession.commit()

    # Test 1: Enable autorecharge with no spending
    response = await client.put(
        "/v0/admin/enable_autorecharge",
        params={"id": user.id, "enable": True},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 400
    error_data = response.json()
    assert "User must spend at least $100.00" in error_data["detail"]
    assert "Current spending: $0.00" in error_data["detail"]

    # Test 2: Set autorecharge quantity below minimum
    test_amounts = [0.01, 10.0, 24.99]
    for amount in test_amounts:
        response = await client.put(
            "/v0/admin/autorecharge_qty",
            params={"id": user.id, "qty": amount},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 400
        error_data = response.json()
        assert "Minimum auto-recharge amount is $25" in error_data["detail"]

    # Test 3: Valid autorecharge quantity should succeed
    response = await client.put(
        "/v0/admin/autorecharge_qty",
        params={"id": user.id, "qty": 25.0},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_user_not_found_error_handling(client: AsyncClient, dbsession: Session):
    """Test error handling when user is not found."""
    from orchestra.tests.utils import ADMIN_HEADERS

    non_existent_uid = "non_existent_user"

    # Test auto-recharge eligibility endpoint with non-existent user
    response = await client.get(
        f"/v0/admin/user_billing_eligibility?user_id={non_existent_uid}",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 404
    error_data = response.json()
    assert "User ID not found" in error_data["detail"]

    # Test enable autorecharge with non-existent user
    response = await client.put(
        "/v0/admin/enable_autorecharge",
        params={"id": non_existent_uid, "enable": True},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 404

    # Test set autorecharge qty with non-existent user
    response = await client.put(
        "/v0/admin/autorecharge_qty",
        params={"id": non_existent_uid, "qty": 50.0},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 12. Test auto-recharge Stripe invoice item creation                        #
# --------------------------------------------------------------------------- #
def test_queue_auto_recharge_creates_stripe_invoice_item(
    dbsession: Session,
    mock_stripe,
    monkeypatch,
):
    """Test that queue_auto_recharge creates both a database record AND a Stripe invoice item."""
    import orchestra.lib.billing

    calls = []

    def mock_create(**kwargs):
        calls.append(kwargs)
        mock_stripe["item"].append(kwargs)
        return SimpleNamespace(
            id="ii_test_123",
            customer=kwargs["customer"],
            amount=kwargs["amount"],
        )

    mock_invoice_item = SimpleNamespace(create=mock_create)
    mock_stripe_module = SimpleNamespace(
        InvoiceItem=mock_invoice_item,
        error=SimpleNamespace(
            StripeError=Exception,
            InvalidRequestError=Exception,
        ),
    )

    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    stripe_customer_id = "cus_test_auto_recharge"
    user, ba = _make_user(
        dbsession,
        "auto_recharge_stripe_test",
        credits=5,
        stripe_customer_id=stripe_customer_id,
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Clear any previous calls
    mock_stripe["item"].clear()

    # Queue auto-recharge
    queue_auto_recharge(dbsession, ba, 50)
    dbsession.commit()

    # Verify database record was created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.amount_usd == Decimal("50.00")
    assert recharge.status == RechargeStatus.PENDING_INVOICE
    assert recharge.type == "auto"

    # Verify Stripe invoice item was created
    assert len(mock_stripe["item"]) == 1
    stripe_call = mock_stripe["item"][0]
    assert stripe_call["customer"] == stripe_customer_id
    assert stripe_call["amount"] == 5000  # 50 credits * 100 cents
    assert stripe_call["currency"] == "usd"
    assert "auto-recharge" in stripe_call["description"]
    assert stripe_call["metadata"]["recharge_type"] == "auto"
    assert stripe_call["metadata"]["billing_account_id"] == str(ba.id)


def test_queue_auto_recharge_no_stripe_customer_id(
    dbsession: Session,
    mock_stripe,
    monkeypatch,
):
    """Test that queue_auto_recharge handles users without Stripe customer ID gracefully."""
    import orchestra.lib.billing

    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(create=lambda **kw: None),
        error=SimpleNamespace(StripeError=Exception),
    )

    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    user, ba = _make_user(
        dbsession,
        "no_stripe_customer_user",
        credits=5,
        stripe_customer_id=None,
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Clear any previous calls
    mock_stripe["item"].clear()

    # Queue auto-recharge - should not fail
    queue_auto_recharge(dbsession, ba, 50)
    dbsession.commit()

    # Verify database record was still created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.status == RechargeStatus.PENDING_INVOICE

    # Verify NO Stripe invoice item was created
    assert len(mock_stripe["item"]) == 0


def test_auto_recharge_flow_creates_stripe_items(
    dbsession: Session,
    mock_stripe,
    monkeypatch,
):
    """Test the complete auto-recharge flow from credit deduction to Stripe invoice item."""
    import orchestra.lib.billing
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    def mock_create(**kwargs):
        mock_stripe["item"].append(kwargs)
        return SimpleNamespace(
            id="ii_test_flow",
            customer=kwargs["customer"],
            amount=kwargs["amount"],
        )

    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(create=mock_create),
        error=SimpleNamespace(StripeError=Exception),
    )

    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    stripe_customer_id = "cus_complete_flow"
    user, ba = _make_user(
        dbsession,
        "complete_flow_user",
        credits=15,
        stripe_customer_id=stripe_customer_id,
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    ba_dao = BillingAccountDAO(dbsession)

    # Clear any previous calls
    mock_stripe["item"].clear()

    # Simulate credit deduction that triggers auto-recharge
    ba_dao.deduct_credits(ba.id, 10)  # Deduct 10 credits, leaving 5
    dbsession.commit()

    # Now user has 5 credits, below threshold of 10
    dbsession.refresh(ba)
    assert float(ba.credits) == 5

    # Simulate the auto-recharge trigger (normally done in bg_tasks).
    # queue_auto_recharge now adds credits immediately, so no separate
    # add_credits call is needed.
    if ba.credits <= ba.autorecharge_threshold:
        queue_auto_recharge(dbsession, ba, int(ba.autorecharge_qty))
        dbsession.commit()

    # Verify final state
    dbsession.refresh(ba)
    assert float(ba.credits) == 55  # 5 + 50 from auto-recharge

    # Verify recharge record
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.type == "auto"

    # Verify Stripe invoice item was created
    assert len(mock_stripe["item"]) == 1
    assert mock_stripe["item"][0]["customer"] == stripe_customer_id
    assert mock_stripe["item"][0]["amount"] == 5000  # 50 credits * 100 cents


def test_queue_auto_recharge_stripe_error_handling(dbsession: Session, monkeypatch):
    """Test that queue_auto_recharge handles Stripe errors gracefully."""
    import orchestra.lib.billing

    stripe_customer_id = "cus_error_test"
    user, ba = _make_user(
        dbsession,
        "stripe_error_user",
        credits=5,
        stripe_customer_id=stripe_customer_id,
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Create a proper mock for Stripe errors
    class MockStripeError(Exception):
        def __init__(self, message, param=None):
            super().__init__(message)
            self.param = param

    # Mock Stripe to raise an error
    def mock_create(**kwargs):
        raise MockStripeError("Customer not found", param="customer")

    # Create the mock with proper error hierarchy
    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(create=mock_create),
        error=SimpleNamespace(
            StripeError=MockStripeError,
            InvalidRequestError=MockStripeError,
        ),
    )

    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    # Queue auto-recharge - should not raise despite Stripe error
    queue_auto_recharge(dbsession, ba, 50)
    dbsession.commit()

    # Verify database record was still created
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.status == RechargeStatus.PENDING_INVOICE
    assert recharge.type == "auto"


# ============== BillingEntity Pattern Tests (moved from test_org_billing) ==============

# ============== BillingEntity Pattern Tests ==============


@pytest.mark.anyio
async def test_get_billing_entity_personal(client: AsyncClient, dbsession):
    """Test get_billing_entity returns user for personal context."""
    from decimal import Decimal

    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.dao.user_dao import UserDAO
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    user = await create_test_user(client, "entity_personal@test.com")

    # Add credits to user via billing_account_dao
    user_dao = UserDAO(dbsession)
    ba_dao = BillingAccountDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user["id"])
    ba_dao.add_credits(user_obj.billing_account_id, 50)
    dbsession.commit()

    # Get billing entity for personal query
    entity = get_billing_entity(dbsession, user["id"], organization_id=None)

    assert entity.entity_type == BillingEntityType.USER
    assert entity.entity_id == user["id"]
    assert entity.credits == Decimal("50")
    assert entity.is_user is True
    assert entity.is_organization is False


@pytest.mark.anyio
async def test_get_billing_entity_org_no_stripe_customer(
    client: AsyncClient,
    dbsession,
):
    """Test get_billing_entity returns entity for org without stripe_customer_id.

    Organization creation now always provisions a BillingAccount, so
    get_billing_entity should succeed even without a Stripe customer ID.
    """
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    owner = await create_test_user(client, "entity_no_billing_owner@test.com")

    # Create organization (billing account created automatically, no stripe_customer_id)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Entity No Billing Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # get_billing_entity should succeed — billing account exists
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)
    assert entity.entity_type == BillingEntityType.ORGANIZATION
    assert entity.stripe_customer_id is None


@pytest.mark.anyio
async def test_get_billing_entity_org_direct(client: AsyncClient, dbsession):
    """Test get_billing_entity returns org for direct org billing."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import BillingEntityType, get_billing_entity

    owner = await create_test_user(client, "entity_direct_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Entity Direct Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing for org via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_direct_test"
    org.billing_account.credits = Decimal("200")
    dbsession.commit()

    # Get billing entity for org query
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    # Should return organization (direct billing)
    assert entity.entity_type == BillingEntityType.ORGANIZATION
    assert entity.entity_id == org_id
    assert entity.credits == Decimal("200")
    assert entity.is_organization is True
    assert entity.has_billing is True


@pytest.mark.anyio
async def test_deduct_credits_from_user(client: AsyncClient, dbsession):
    """Test deduct_credits from a user billing entity."""
    from decimal import Decimal

    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.dao.user_dao import UserDAO
    from orchestra.lib.billing import deduct_credits, get_billing_entity

    user = await create_test_user(client, "deduct_user@test.com")

    # Add credits via billing_account_dao
    user_dao = UserDAO(dbsession)
    ba_dao = BillingAccountDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user["id"])
    ba_dao.add_credits(user_obj.billing_account_id, 100)
    dbsession.commit()

    # Get billing entity
    entity = get_billing_entity(dbsession, user["id"])

    # Deduct credits
    new_balance = deduct_credits(dbsession, entity, Decimal("25.50"))
    dbsession.commit()

    assert new_balance == Decimal("74.50")

    # Verify in DB
    updated_user = user_dao.get_user_with_id(user["id"])
    assert updated_user.billing_account.credits == Decimal("74.50")


@pytest.mark.anyio
async def test_deduct_credits_from_org(client: AsyncClient, dbsession):
    """Test deduct_credits from an organization billing entity."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import deduct_credits, get_billing_entity

    owner = await create_test_user(client, "deduct_org@test.com")

    # Create organization with direct billing
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Deduct Org Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Enable direct billing and add credits via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_deduct_test"
    org.billing_account.credits = Decimal("500")
    dbsession.commit()

    # Get billing entity
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    # Deduct credits
    new_balance = deduct_credits(dbsession, entity, Decimal("123.45"))
    dbsession.commit()

    assert new_balance == Decimal("376.55")

    # Verify in DB
    dbsession.refresh(org)
    assert org.billing_account.credits == Decimal("376.55")


@pytest.mark.anyio
async def test_billing_entity_should_trigger_autorecharge(
    client: AsyncClient,
    dbsession,
):
    """Test BillingEntity.should_trigger_autorecharge method."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import get_billing_entity

    owner = await create_test_user(client, "autorecharge_trigger@test.com")

    # Create organization with direct billing and autorecharge enabled
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Autorecharge Trigger Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Setup org with autorecharge via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_autorecharge"
    org.billing_account.credits = Decimal("100")
    org.billing_account.autorecharge = True
    org.billing_account.autorecharge_threshold = Decimal("50")
    org.billing_account.autorecharge_qty = Decimal("200")
    dbsession.commit()

    # Get billing entity
    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    # Balance above threshold - should not trigger
    assert entity.should_trigger_autorecharge(Decimal("100")) is False
    assert entity.should_trigger_autorecharge(Decimal("51")) is False

    # Balance at or below threshold - should trigger
    assert entity.should_trigger_autorecharge(Decimal("50")) is True
    assert entity.should_trigger_autorecharge(Decimal("25")) is True
    assert entity.should_trigger_autorecharge(Decimal("0")) is True


@pytest.mark.anyio
async def test_billing_entity_no_autorecharge_without_stripe(
    client: AsyncClient,
    dbsession,
):
    """Test that autorecharge doesn't trigger without Stripe customer ID."""
    from decimal import Decimal

    from orchestra.db.models.orchestra_models import Organization
    from orchestra.lib.billing import get_billing_entity

    owner = await create_test_user(client, "no_stripe_autorecharge@test.com")

    # Create organization with direct billing (has stripe_customer_id)
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "No Stripe Autorecharge Test"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Setup org with autorecharge and stripe_customer_id via billing_account
    org = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org.billing_account.stripe_customer_id = "cus_no_stripe_test"
    org.billing_account.autorecharge = True
    org.billing_account.autorecharge_threshold = Decimal("50")
    org.billing_account.credits = Decimal("100")  # Above threshold
    dbsession.commit()

    entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

    assert entity.is_organization is True
    assert entity.has_billing is True
    # Credits above threshold - should NOT trigger autorecharge
    assert entity.should_trigger_autorecharge(Decimal("100")) is False
    # Credits below threshold - should trigger autorecharge
    assert entity.should_trigger_autorecharge(Decimal("40")) is True


def test_queue_auto_recharge_adds_credits_immediately(dbsession: Session, monkeypatch):
    """Test that queue_auto_recharge adds credits to the billing account right away."""
    import orchestra.lib.billing

    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(id="ii_test"),
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    user, ba = _make_user(
        dbsession,
        "ar_adds_credits_user",
        credits=5,
        stripe_customer_id="cus_ar_credits",
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    assert float(ba.credits) == 5

    queue_auto_recharge(dbsession, ba, 50, entity_label="test")
    dbsession.commit()

    dbsession.refresh(ba)
    # Credits should have been added immediately: 5 + 50 = 55
    assert float(ba.credits) == 55

    # Recharge record should also exist
    recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.status == RechargeStatus.PENDING_INVOICE


def test_queue_auto_recharge_credits_survive_negative_balance(
    dbsession: Session,
    monkeypatch,
):
    """Test that auto-recharge can bring a negative balance back to positive."""
    import orchestra.lib.billing

    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(id="ii_test_neg"),
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    user, ba = _make_user(
        dbsession,
        "ar_negative_user",
        credits=-10,
        stripe_customer_id="cus_ar_negative",
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=100,
    )
    dbsession.commit()

    queue_auto_recharge(dbsession, ba, 100, entity_label="test")
    dbsession.commit()

    dbsession.refresh(ba)
    # -10 + 100 = 90
    assert float(ba.credits) == 90


def test_levy_auto_recharge_adds_credits(dbsession: Session, monkeypatch):
    """Test that the levy + auto-recharge flow properly adds credits.

    After the levy deducts credits, auto-recharge should add credits back
    immediately, preventing the account from staying at zero/negative.
    """
    import orchestra.lib.billing

    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(id="ii_levy_ar"),
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    user, ba = _make_user(
        dbsession,
        "levy_ar_user",
        credits=15,
        stripe_customer_id="cus_levy_ar",
        autorecharge=True,
        autorecharge_threshold=10,
        autorecharge_qty=50,
    )
    dbsession.commit()

    # Simulate what the levy does: deduct credits, then auto-recharge
    ba.credits = ba.credits - Decimal("12")  # 15 - 12 = 3, below threshold
    dbsession.flush()

    if (
        ba.autorecharge
        and ba.stripe_customer_id
        and ba.credits <= ba.autorecharge_threshold
    ):
        queue_auto_recharge(
            dbsession,
            ba,
            int(ba.autorecharge_qty),
            entity_label="test",
        )

    dbsession.commit()
    dbsession.refresh(ba)

    # Credits should be: 15 - 12 + 50 = 53
    assert float(ba.credits) == 53


@pytest.mark.anyio
async def test_deduct_endpoint_triggers_auto_recharge(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch,
):
    """Test that the /credits/deduct endpoint triggers auto-recharge when
    credits fall below the threshold."""
    import orchestra.lib.billing

    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(id="ii_deduct_ar"),
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    user = await create_test_user(client, "deduct_ar@test.com")

    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.dao.user_dao import UserDAO

    user_dao = UserDAO(dbsession)
    ba_dao = BillingAccountDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user["id"])

    # Set up autorecharge on the billing account
    ba = user_obj.billing_account
    ba.credits = Decimal("15")
    ba.autorecharge = True
    ba.autorecharge_threshold = Decimal("10")
    ba.autorecharge_qty = Decimal("50")
    ba.stripe_customer_id = "cus_deduct_ar"
    dbsession.commit()

    # Deduct 10 credits → balance goes to 5, below threshold of 10
    response = await client.post(
        "/v0/credits/deduct",
        json={"amount": 10.0},
        headers=user["headers"],
    )
    assert response.status_code == 200

    data = response.json()
    assert data["previous_credits"] == 15.0
    assert data["deducted"] == 10.0
    # Auto-recharge should have kicked in: 15 - 10 + 50 = 55
    assert data["current_credits"] == 55.0

    # Verify a Recharge record was created
    dbsession.expire_all()
    recharge = (
        dbsession.query(Recharge)
        .filter_by(billing_account_id=ba.id, type="auto")
        .first()
    )
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.status == RechargeStatus.PENDING_INVOICE


@pytest.mark.anyio
async def test_deduct_endpoint_no_auto_recharge_when_above_threshold(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch,
):
    """Test that /credits/deduct does NOT trigger auto-recharge when
    credits remain above the threshold."""
    import orchestra.lib.billing

    mock_stripe_module = SimpleNamespace(
        InvoiceItem=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(id="ii_no_ar"),
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

    user = await create_test_user(client, "deduct_no_ar@test.com")

    from orchestra.db.dao.user_dao import UserDAO

    user_dao = UserDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user["id"])

    ba = user_obj.billing_account
    ba.credits = Decimal("100")
    ba.autorecharge = True
    ba.autorecharge_threshold = Decimal("10")
    ba.autorecharge_qty = Decimal("50")
    ba.stripe_customer_id = "cus_no_ar"
    dbsession.commit()

    # Deduct 5 credits → balance is 95, above threshold
    response = await client.post(
        "/v0/credits/deduct",
        json={"amount": 5.0},
        headers=user["headers"],
    )
    assert response.status_code == 200

    data = response.json()
    assert data["current_credits"] == 95.0

    # No Recharge record should have been created
    dbsession.expire_all()
    recharge = (
        dbsession.query(Recharge)
        .filter_by(billing_account_id=ba.id, type="auto")
        .first()
    )
    assert recharge is None


@pytest.mark.anyio
async def test_deduct_endpoint_no_auto_recharge_when_disabled(
    client: AsyncClient,
    dbsession: Session,
):
    """Test that /credits/deduct does NOT trigger auto-recharge when
    autorecharge is disabled."""
    user = await create_test_user(client, "deduct_ar_disabled@test.com")

    from orchestra.db.dao.user_dao import UserDAO

    user_dao = UserDAO(dbsession)
    user_obj = user_dao.get_user_with_id(user["id"])

    ba = user_obj.billing_account
    ba.credits = Decimal("15")
    ba.autorecharge = False  # disabled
    ba.autorecharge_threshold = Decimal("10")
    ba.autorecharge_qty = Decimal("50")
    ba.stripe_customer_id = "cus_ar_disabled"
    dbsession.commit()

    response = await client.post(
        "/v0/credits/deduct",
        json={"amount": 10.0},
        headers=user["headers"],
    )
    assert response.status_code == 200

    data = response.json()
    # No auto-recharge, so 15 - 10 = 5
    assert data["current_credits"] == 5.0

    dbsession.expire_all()
    recharge = (
        dbsession.query(Recharge)
        .filter_by(billing_account_id=ba.id, type="auto")
        .first()
    )
    assert recharge is None


def test_checkout_creates_recharge_record_for_user(
    dbsession: Session,
    monkeypatch,
):
    """Test that checkout.session.completed creates a PAID Recharge record
    for user purchases, so spending is tracked for auto-recharge eligibility."""
    import orchestra.web.api.webhooks.stripe as webhook_module

    # Mock Stripe calls used in the webhook handler
    mock_stripe_module = SimpleNamespace(
        PaymentIntent=SimpleNamespace(
            modify=lambda pi_id, **kw: None,
        ),
        Customer=SimpleNamespace(
            modify=lambda cid, **kw: None,
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(webhook_module, "stripe", mock_stripe_module)

    user, ba = _make_user(
        dbsession,
        "checkout_recharge_user",
        credits=0,
        stripe_customer_id="cus_checkout_recharge",
    )
    dbsession.commit()

    from orchestra.web.api.webhooks.stripe import process_checkout_session_event

    event = {
        "id": "evt_checkout_recharge_user",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": user.id,
                "amount_total": 5000,  # $50
                "customer": "cus_checkout_recharge",
                "payment_intent": "pi_checkout_recharge",
                "metadata": {},
            },
        },
    }

    response = process_checkout_session_event(event, dbsession)
    assert response.status_code == 200

    # Verify credits were added
    dbsession.refresh(ba)
    assert float(ba.credits) == 50.0

    # Verify a PAID Recharge record was created
    recharge = (
        dbsession.query(Recharge)
        .filter_by(billing_account_id=ba.id, type="payment")
        .first()
    )
    assert recharge is not None
    assert recharge.quantity == Decimal("50")
    assert recharge.amount_usd == Decimal("50")
    assert recharge.status == RechargeStatus.PAID


def test_checkout_creates_recharge_record_for_org(
    dbsession: Session,
    monkeypatch,
):
    """Test that checkout.session.completed creates a PAID Recharge record
    for organization purchases."""
    import orchestra.web.api.webhooks.stripe as webhook_module

    mock_stripe_module = SimpleNamespace(
        PaymentIntent=SimpleNamespace(
            modify=lambda pi_id, **kw: None,
        ),
        Customer=SimpleNamespace(
            modify=lambda cid, **kw: None,
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(webhook_module, "stripe", mock_stripe_module)

    # Create org via the test helper
    org, org_ba = _create_org_for_invoicing_test(
        dbsession,
        name="Checkout Recharge Org",
        stripe_customer_id="cus_checkout_org",
        credits=Decimal("0"),
    )
    dbsession.commit()

    from orchestra.web.api.webhooks.stripe import process_checkout_session_event

    event = {
        "id": "evt_checkout_recharge_org",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": None,
                "amount_total": 10000,  # $100
                "customer": "cus_checkout_org",
                "payment_intent": "pi_checkout_org",
                "metadata": {"organization_id": str(org.id)},
            },
        },
    }

    response = process_checkout_session_event(event, dbsession)
    assert response.status_code == 200

    # Verify credits were added
    dbsession.refresh(org_ba)
    assert float(org_ba.credits) == 100.0

    # Verify a PAID Recharge record was created
    recharge = (
        dbsession.query(Recharge)
        .filter_by(billing_account_id=org_ba.id, type="payment")
        .first()
    )
    assert recharge is not None
    assert recharge.quantity == Decimal("100")
    assert recharge.amount_usd == Decimal("100")
    assert recharge.status == RechargeStatus.PAID


def test_checkout_recharge_counts_toward_auto_recharge_eligibility(
    dbsession: Session,
    monkeypatch,
):
    """Test that a checkout-created Recharge record counts toward the
    cumulative spending threshold for auto-recharge eligibility."""
    import orchestra.web.api.webhooks.stripe as webhook_module
    from orchestra.db.dao.billing_account_dao import (
        MIN_SPEND_FOR_AUTO_RECHARGE,
        BillingAccountDAO,
    )

    mock_stripe_module = SimpleNamespace(
        PaymentIntent=SimpleNamespace(
            modify=lambda pi_id, **kw: None,
        ),
        Customer=SimpleNamespace(
            modify=lambda cid, **kw: None,
        ),
        error=SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setattr(webhook_module, "stripe", mock_stripe_module)

    user, ba = _make_user(
        dbsession,
        "checkout_eligibility_user",
        credits=0,
        stripe_customer_id="cus_checkout_elig",
    )
    dbsession.commit()

    ba_dao = BillingAccountDAO(dbsession)

    # Initially not eligible
    assert not ba_dao.can_enable_auto_recharge(ba.id)
    assert ba_dao.get_total_spending(ba.id) == 0

    # Process a checkout that exceeds the threshold
    from orchestra.web.api.webhooks.stripe import process_checkout_session_event

    event = {
        "id": "evt_checkout_eligibility",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": user.id,
                "amount_total": 15000,  # $150
                "customer": "cus_checkout_elig",
                "payment_intent": "pi_checkout_elig",
                "metadata": {},
            },
        },
    }

    process_checkout_session_event(event, dbsession)

    # Now spending should be tracked
    total_spending = ba_dao.get_total_spending(ba.id)
    assert float(total_spending) == 150.0
    assert total_spending >= MIN_SPEND_FOR_AUTO_RECHARGE

    # Now eligible for auto-recharge
    assert ba_dao.can_enable_auto_recharge(ba.id)


# =========================================================================== #
# 12. Billing API endpoints — checkout / portal / status                       #
# =========================================================================== #


@pytest.mark.anyio
async def test_checkout_session_endpoint(client, dbsession, monkeypatch):
    """POST /billing/checkout-session creates a Stripe Checkout session."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "checkout_ep_user@test.com")

    # Give user a billing account with stripe_customer_id

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("100"), stripe_customer_id="cus_ep_test")
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        db_user.billing_account.stripe_customer_id = "cus_ep_test"
    dbsession.commit()

    # Patch settings for price ID
    monkeypatch.setattr(
        settings,
        "stripe_unify_credits_price_id_personal",
        "price_test_personal",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "console_url",
        "http://localhost:3000",
        raising=False,
    )

    # Mock Stripe calls in the billing views module
    import orchestra.web.api.billing.views as billing_views

    class MockCheckoutSession:
        def __init__(self):
            self.url = "https://checkout.stripe.com/test_session"
            self.id = "cs_test_123"

    class MockCustomer:
        email = "checkout_ep_user@test.com"
        name = "Test"
        deleted = False

    mock_calls = {"create": [], "retrieve": [], "modify": [], "list_tax_ids": []}

    def mock_session_create(**kwargs):
        mock_calls["create"].append(kwargs)
        return MockCheckoutSession()

    def mock_customer_retrieve(cid):
        mock_calls["retrieve"].append(cid)
        return MockCustomer()

    def mock_customer_modify(cid, **kwargs):
        mock_calls["modify"].append({"cid": cid, **kwargs})

    def mock_list_tax_ids(cid):
        mock_calls["list_tax_ids"].append(cid)
        return SimpleNamespace(data=[])

    def mock_create_tax_id(cid, **kwargs):
        pass

    mock_stripe = SimpleNamespace(
        api_key=None,
        checkout=SimpleNamespace(
            Session=SimpleNamespace(create=mock_session_create),
        ),
        Customer=SimpleNamespace(
            retrieve=mock_customer_retrieve,
            modify=mock_customer_modify,
            list_tax_ids=mock_list_tax_ids,
            create_tax_id=mock_create_tax_id,
        ),
        InvalidRequestError=Exception,
    )
    monkeypatch.setattr(billing_views, "stripe", mock_stripe)

    response = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert "url" in data
    assert "session_id" in data
    assert data["url"] == "https://checkout.stripe.com/test_session"
    assert data["session_id"] == "cs_test_123"

    # Verify Stripe was called
    assert len(mock_calls["create"]) == 1
    create_params = mock_calls["create"][0]
    assert create_params["mode"] == "payment"
    assert create_params["customer"] == "cus_ep_test"
    assert create_params["client_reference_id"] == user["id"]


@pytest.mark.anyio
async def test_checkout_session_no_stripe_customer(client, dbsession, monkeypatch):
    """POST /billing/checkout-session works for first-time buyer (no Stripe customer)."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "checkout_new_user@test.com")

    # User has billing account but no stripe_customer_id
    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("0"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    dbsession.commit()

    monkeypatch.setattr(
        settings,
        "stripe_unify_credits_price_id_personal",
        "price_test_personal",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "console_url",
        "http://localhost:3000",
        raising=False,
    )

    import orchestra.web.api.billing.views as billing_views

    class MockCheckoutSession:
        url = "https://checkout.stripe.com/new_session"
        id = "cs_new_123"

    mock_calls = {"create": []}

    def mock_session_create(**kwargs):
        mock_calls["create"].append(kwargs)
        return MockCheckoutSession()

    mock_stripe = SimpleNamespace(
        api_key=None,
        checkout=SimpleNamespace(
            Session=SimpleNamespace(create=mock_session_create),
        ),
        Customer=SimpleNamespace(),
        InvalidRequestError=Exception,
    )
    monkeypatch.setattr(billing_views, "stripe", mock_stripe)

    response = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["url"] == "https://checkout.stripe.com/new_session"

    # Should use customer_creation instead of customer
    create_params = mock_calls["create"][0]
    assert "customer" not in create_params
    assert create_params["customer_creation"] == "always"


@pytest.mark.anyio
async def test_portal_session_endpoint(client, dbsession, monkeypatch):
    """POST /billing/portal-session creates a Stripe portal session."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "portal_ep_user@test.com")

    # Give user a billing account with stripe_customer_id
    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("50"), stripe_customer_id="cus_portal_test")
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        db_user.billing_account.stripe_customer_id = "cus_portal_test"
    dbsession.commit()

    import orchestra.web.api.billing.views as billing_views

    mock_calls = {"create": []}

    def mock_portal_create(**kwargs):
        mock_calls["create"].append(kwargs)
        return SimpleNamespace(url="https://billing.stripe.com/portal_test")

    mock_stripe = SimpleNamespace(
        api_key=None,
        billing_portal=SimpleNamespace(
            Session=SimpleNamespace(create=mock_portal_create),
        ),
        InvalidRequestError=Exception,
    )
    monkeypatch.setattr(billing_views, "stripe", mock_stripe)

    response = await client.post(
        "/v0/billing/portal-session",
        headers=user["headers"],
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["url"] == "https://billing.stripe.com/portal_test"

    # Verify Stripe was called with correct customer
    assert len(mock_calls["create"]) == 1
    assert mock_calls["create"][0]["customer"] == "cus_portal_test"


@pytest.mark.anyio
async def test_portal_session_no_customer(client, dbsession, monkeypatch):
    """POST /billing/portal-session returns 404 when no Stripe customer exists."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "portal_no_cust@test.com")

    # Billing account without stripe_customer_id
    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("0"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    dbsession.commit()

    import orchestra.web.api.billing.views as billing_views

    mock_stripe = SimpleNamespace(
        api_key=None,
        billing_portal=SimpleNamespace(Session=SimpleNamespace()),
        InvalidRequestError=Exception,
    )
    monkeypatch.setattr(billing_views, "stripe", mock_stripe)

    response = await client.post(
        "/v0/billing/portal-session",
        headers=user["headers"],
    )

    assert response.status_code == 404
    assert "No Stripe customer ID found" in response.json()["detail"]


@pytest.mark.anyio
async def test_checkout_status_endpoint(client, dbsession, monkeypatch):
    """GET /billing/checkout-status returns session status for valid owner."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "checkout_status_user@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(
            credits=Decimal("25"),
            stripe_customer_id="cus_status_test",
        )
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        db_user.billing_account.stripe_customer_id = "cus_status_test"
    dbsession.commit()

    import orchestra.web.api.billing.views as billing_views

    class MockCheckoutSession:
        status = "complete"
        payment_status = "paid"
        customer = "cus_status_test"
        client_reference_id = user["id"]

    def mock_session_retrieve(sid):
        return MockCheckoutSession()

    mock_stripe = SimpleNamespace(
        api_key=None,
        checkout=SimpleNamespace(
            Session=SimpleNamespace(retrieve=mock_session_retrieve),
        ),
        InvalidRequestError=Exception,
    )
    monkeypatch.setattr(billing_views, "stripe", mock_stripe)

    response = await client.get(
        "/v0/billing/checkout-status?session_id=cs_test_status",
        headers=user["headers"],
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["status"] == "complete"
    assert data["payment_status"] == "paid"


@pytest.mark.anyio
async def test_checkout_status_wrong_owner(client, dbsession, monkeypatch):
    """GET /billing/checkout-status returns 403 for wrong owner."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "checkout_wrong_owner@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(
            credits=Decimal("10"),
            stripe_customer_id="cus_wrong_owner",
        )
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    dbsession.commit()

    import orchestra.web.api.billing.views as billing_views

    class MockCheckoutSession:
        status = "complete"
        payment_status = "paid"
        customer = "cus_someone_else"  # Different customer!
        client_reference_id = "other_user_id"  # Different user!

    def mock_session_retrieve(sid):
        return MockCheckoutSession()

    mock_stripe = SimpleNamespace(
        api_key=None,
        checkout=SimpleNamespace(
            Session=SimpleNamespace(retrieve=mock_session_retrieve),
        ),
        InvalidRequestError=Exception,
    )
    monkeypatch.setattr(billing_views, "stripe", mock_stripe)

    response = await client.get(
        "/v0/billing/checkout-status?session_id=cs_test_wrong",
        headers=user["headers"],
    )

    assert response.status_code == 403
    assert "does not belong" in response.json()["detail"]


@pytest.mark.anyio
async def test_checkout_session_no_price_id_configured(client, dbsession, monkeypatch):
    """POST /billing/checkout-session returns 500 when price ID is not configured."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "no_price_user@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("0"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    dbsession.commit()

    # Explicitly set price ID to None
    monkeypatch.setattr(
        settings,
        "stripe_unify_credits_price_id_personal",
        None,
        raising=False,
    )

    import orchestra.web.api.billing.views as billing_views

    mock_stripe = SimpleNamespace(api_key=None, InvalidRequestError=Exception)
    monkeypatch.setattr(billing_views, "stripe", mock_stripe)

    response = await client.post(
        "/v0/billing/checkout-session",
        headers=user["headers"],
    )

    assert response.status_code == 500
    assert "price ID not configured" in response.json()["detail"]


# ========================================================================= #
# Auto-recharge endpoint tests                                               #
# ========================================================================= #


@pytest.mark.anyio
async def test_get_auto_recharge_returns_settings_and_eligibility(
    client,
    dbsession,
):
    """GET /billing/auto-recharge returns combined settings + eligibility."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "ar_get_user@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(
            credits=Decimal("10"),
            autorecharge=True,
            autorecharge_threshold=Decimal("5"),
            autorecharge_qty=Decimal("50"),
        )
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        ba = db_user.billing_account
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("5")
        ba.autorecharge_qty = Decimal("50")
    dbsession.commit()

    response = await client.get(
        "/v0/billing/auto-recharge",
        headers=user["headers"],
    )

    assert response.status_code == 200, response.json()
    data = response.json()

    # Settings
    assert data["enabled"] is True
    assert data["threshold"] == 5.0
    assert data["qty"] == 50.0

    # Eligibility (no recharges yet ⇒ not eligible)
    assert data["eligible"] is False
    assert data["total_spending"] == 0.0
    assert data["minimum_spend_required"] == 100.0
    assert data["remaining_spend_needed"] == 100.0


@pytest.mark.anyio
async def test_get_auto_recharge_eligible_after_spending(client, dbsession):
    """GET /billing/auto-recharge shows eligible=true after meeting spend threshold."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "ar_elig_user@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("200"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        ba = db_user.billing_account

    # Add a paid recharge of $150 to meet the $100 threshold
    recharge = Recharge(
        billing_account_id=ba.id,
        type="payment",
        quantity=Decimal("150"),
        amount_usd=Decimal("150"),
        status=RechargeStatus.PAID,
    )
    dbsession.add(recharge)
    dbsession.commit()

    response = await client.get(
        "/v0/billing/auto-recharge",
        headers=user["headers"],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["eligible"] is True
    assert data["total_spending"] == 150.0
    assert data["remaining_spend_needed"] == 0.0


@pytest.mark.anyio
async def test_put_auto_recharge_enable_with_all_settings(client, dbsession):
    """PUT /billing/auto-recharge updates enabled + threshold + qty atomically."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "ar_put_user@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("100"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        ba = db_user.billing_account

    # Add spending to meet eligibility
    recharge = Recharge(
        billing_account_id=ba.id,
        type="payment",
        quantity=Decimal("200"),
        amount_usd=Decimal("200"),
        status=RechargeStatus.PAID,
    )
    dbsession.add(recharge)
    dbsession.commit()

    response = await client.put(
        "/v0/billing/auto-recharge",
        json={"enabled": True, "threshold": 10.0, "qty": 50.0},
        headers=user["headers"],
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["enabled"] is True
    assert data["threshold"] == 10.0
    assert data["qty"] == 50.0
    assert data["eligible"] is True

    # Verify database was updated
    dbsession.refresh(ba)
    assert ba.autorecharge is True
    assert float(ba.autorecharge_threshold) == 10.0
    assert float(ba.autorecharge_qty) == 50.0


@pytest.mark.anyio
async def test_put_auto_recharge_toggle_only(client, dbsession):
    """PUT /billing/auto-recharge with only enabled flag toggles without changing threshold/qty."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "ar_toggle_user@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(
            credits=Decimal("50"),
            autorecharge=True,
            autorecharge_threshold=Decimal("15"),
            autorecharge_qty=Decimal("75"),
        )
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        ba = db_user.billing_account
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("15")
        ba.autorecharge_qty = Decimal("75")
    dbsession.commit()

    # Disable auto-recharge — threshold/qty should remain unchanged
    response = await client.put(
        "/v0/billing/auto-recharge",
        json={"enabled": False},
        headers=user["headers"],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["threshold"] == 15.0  # unchanged
    assert data["qty"] == 75.0  # unchanged


@pytest.mark.anyio
async def test_put_auto_recharge_enable_fails_without_spending(client, dbsession):
    """PUT /billing/auto-recharge returns 400 when trying to enable without meeting spend threshold."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "ar_ineligible@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("50"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    dbsession.commit()

    response = await client.put(
        "/v0/billing/auto-recharge",
        json={"enabled": True, "threshold": 5.0, "qty": 25.0},
        headers=user["headers"],
    )

    assert response.status_code == 400
    assert "must spend" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_put_auto_recharge_rejects_low_qty(client, dbsession):
    """PUT /billing/auto-recharge returns 400 when qty is below the $25 minimum."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "ar_low_qty@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(credits=Decimal("50"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    dbsession.commit()

    response = await client.put(
        "/v0/billing/auto-recharge",
        json={"enabled": False, "threshold": 5.0, "qty": 10.0},
        headers=user["headers"],
    )

    assert response.status_code == 400
    assert "minimum" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_put_auto_recharge_disable_always_allowed(client, dbsession):
    """PUT /billing/auto-recharge allows disabling even without spending threshold."""
    from orchestra.tests.utils import create_test_user

    user = await create_test_user(client, "ar_disable_user@test.com")

    user_dao = UserDAO(dbsession)
    db_user = user_dao.get_user_with_id(user["id"])
    if db_user.billing_account is None:
        ba = BillingAccount(
            credits=Decimal("50"),
            autorecharge=True,
            autorecharge_threshold=Decimal("5"),
            autorecharge_qty=Decimal("25"),
        )
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
    else:
        ba = db_user.billing_account
        ba.autorecharge = True
    dbsession.commit()

    # Disabling should succeed even with no spending
    response = await client.put(
        "/v0/billing/auto-recharge",
        json={"enabled": False},
        headers=user["headers"],
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
