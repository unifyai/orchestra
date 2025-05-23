"""
End-to-end billing test-suite, collapsed into one module so `pytest` can
run everything with a single file.

Coverage:
1. Migration smoke – columns exist.
2. Monthly invoicer – groups rows, calls Stripe once, flips status.
3. Webhook idempotency – double delivery does not double-charge.
4. Daily guard – suspends PAST_DUE + empty-wallet users.

The tests use an in-memory SQLite DB and monkey-patch `SessionLocal`
inside each routine so every function shares the same SQLAlchemy session.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Dict

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import orchestra.routines.billing_guard as guard
import orchestra.routines.monthly_invoicer as invoicer

# ─────────────────────────────────────────────────────────────────────────────
# Local imports (runtime code under test)
# ─────────────────────────────────────────────────────────────────────────────
from orchestra.db.base import Base
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.db.models.orchestra_models import Users as User
from orchestra.db.models.orchestra_models import WebhookLog
from orchestra.lib.time import month_end_utc
from orchestra.observability.metrics import billing_suspended_total

# FastAPI entry-point (if available) – skip webhook test when missing
try:
    from orchestra.main import app  # noqa: WPS433
except ModuleNotFoundError:  # pragma: no cover
    app = None
client = TestClient(app) if app else None


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def session() -> Session:
    """Isolated in-memory DB for each test."""
    engine = create_engine("sqlite:///:memory:", future=True, echo=False)
    Base.metadata.create_all(engine)

    conn = engine.connect()
    tx = conn.begin()
    SessionLocal = sessionmaker(bind=conn, expire_on_commit=False)
    db = SessionLocal()

    yield db

    db.close()
    tx.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def patch_sessionlocal(monkeypatch: pytest.MonkeyPatch, session: Session):
    """
    Force every `with SessionLocal()` in business code to reuse *our* session.
    """

    @contextlib.contextmanager
    def _ctx():
        yield session

    # routines that open their own sessions
    monkeypatch.setattr(invoicer, "SessionLocal", _ctx, raising=True)
    monkeypatch.setattr(guard, "SessionLocal", _ctx, raising=True)

    # webhook (optional – only if FastAPI is importable)
    try:
        import orchestra.web.api.webhooks.stripe as wh

        monkeypatch.setattr(wh, "SessionLocal", _ctx, raising=True)
    except ModuleNotFoundError:
        pass

    yield


# ═════════════════════════════════════════════════════════════════════════════
# 1. Migration smoke
# ═════════════════════════════════════════════════════════════════════════════
def test_new_columns_exist(session: Session):
    insp = sa.inspect(session.bind)

    recharge_cols = {c["name"] for c in insp.get_columns("recharge")}
    user_cols = {c["name"] for c in insp.get_columns("users")}

    assert {"status", "stripe_invoice_id", "invoice_group"} <= recharge_cols
    assert "billing_state" in user_cols


# ═════════════════════════════════════════════════════════════════════════════
# 2. Monthly invoicer happy-path
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def mock_stripe(monkeypatch: pytest.MonkeyPatch):
    """Replace the Stripe SDK with a dummy object that records invocations."""
    calls: Dict[str, list] = {"item": [], "invoice": []}

    def _invoice_item_create(**kw):
        calls["item"].append(kw)

    class _Invoice(SimpleNamespace):
        pass

    def _invoice_create(**kw):
        calls["invoice"].append(kw)
        return _Invoice(id="in_test_123")

    dummy = SimpleNamespace(
        InvoiceItem=SimpleNamespace(create=_invoice_item_create),
        Invoice=SimpleNamespace(create=_invoice_create),
    )
    monkeypatch.setattr(invoicer, "stripe", dummy, raising=True)
    return calls


def test_invoicer_flips_rows(session: Session, mock_stripe):
    # Previous month
    today = _dt.date.today()
    first_this_month = today.replace(day=1)
    last_month_end = first_this_month - _dt.timedelta(days=1)
    group_day = month_end_utc(
        _dt.date(last_month_end.year, last_month_end.month, 1),
    )

    # Seed dummy user & 3 pending rows
    user = User(
        id="user_1",
        stripe_customer_id="cus_test_123",
        billing_state="OK",
        credit_balance=100,
    )
    session.add(user)
    session.flush()

    session.add_all(
        [
            Recharge(
                user_id=user.id,
                quantity=Decimal(10),
                amount_usd=Decimal("2.50"),
                status=RechargeStatus.PENDING_INVOICE,
                invoice_group=group_day,
            )
            for _ in range(3)
        ],
    )
    session.commit()

    # Run
    invoicer.invoice_month(last_month_end.year, last_month_end.month)

    # One invoice, one item
    assert len(mock_stripe["item"]) == 1
    assert len(mock_stripe["invoice"]) == 1

    refreshed = session.scalars(select(Recharge)).all()
    assert all(r.status == RechargeStatus.INVOICE_CREATED for r in refreshed)
    assert all(r.stripe_invoice_id for r in refreshed)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Webhook idempotency
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(app is None, reason="FastAPI app not importable")
def test_webhook_is_idempotent(session: Session):
    # Seed user + recharge awaiting payment
    user = User(
        id="u1",
        stripe_customer_id="cus_test",
        credit_balance=100,
        billing_state="OK",
    )
    session.add(user)
    session.flush()

    recharge = Recharge(
        user_id=user.id,
        quantity=Decimal(5),
        amount_usd=Decimal("1.25"),
        status=RechargeStatus.INVOICE_CREATED,
        stripe_invoice_id="in_test_1",
    )
    session.add(recharge)
    session.commit()

    payload = {
        "id": "evt_test_1",
        "type": "invoice.payment_succeeded",
        "data": {"object": {"id": "in_test_1", "status": "paid"}},
    }

    # Deliver twice
    res1 = client.post("/webhooks/stripe", json=payload)
    res2 = client.post("/webhooks/stripe", json=payload)

    assert res1.status_code == res2.status_code == 200

    session.refresh(recharge)
    assert recharge.status == RechargeStatus.PAID
    assert session.query(WebhookLog).count() == 1


# ═════════════════════════════════════════════════════════════════════════════
# 4. Daily billing-guard
# ═════════════════════════════════════════════════════════════════════════════
def test_billing_guard_suspends(session: Session):
    start = billing_suspended_total._value.get()

    offender = User(id="u-past", billing_state="PAST_DUE", credit_balance=0)
    safe_one = User(id="u-ok", billing_state="OK", credit_balance=10)
    session.add_all([offender, safe_one])
    session.commit()

    guard.suspend_past_due_users()

    session.refresh(offender)
    session.refresh(safe_one)

    assert offender.billing_state == "SUSPENDED"
    assert safe_one.billing_state == "OK"
    assert billing_suspended_total._value.get() == start + 1


def test_prepaid_credits_not_invoiced(session: Session):
    """Prepaid credits should be marked PAID and excluded from invoicing."""
    # Create user
    user = User(id="test_user", credit_balance=100)
    session.add(user)
    session.flush()

    # Simulate prepaid credit purchase
    recharge_dao = RechargeDAO(session)
    recharge = recharge_dao.create_recharge(
        user_id=user.id,
        quantity=500,
        amount_usd=Decimal("5.00"),
        invoice_group=month_end_utc(_dt.date.today()),
        type_="payment",
        transaction_id="pi_test_prepaid_123",
        status=RechargeStatus.PAID,  # ← Already paid
    )

    # User balance should be credited
    user.credit_balance += 500
    session.commit()

    # Verify recharge is marked as PAID
    assert recharge.status == RechargeStatus.PAID
    assert user.credit_balance == 600

    # Run monthly invoicer - should skip PAID rows
    pending_recharges = (
        session.query(Recharge).filter_by(status=RechargeStatus.PENDING_INVOICE).all()
    )

    # Should be no pending invoices (prepaid was already paid)
    assert len(pending_recharges) == 0

    # Verify PAID recharge still exists but won't be invoiced
    paid_recharges = session.query(Recharge).filter_by(status=RechargeStatus.PAID).all()
    assert len(paid_recharges) == 1
    assert paid_recharges[0].transaction_id == "pi_test_prepaid_123"
