"""
Integration-style billing tests (matching test_credits pattern).

1. Schema smoke – columns exist.
2. Invoicer – aggregates rows, hits Stripe once, flips status.
3. Webhook idempotency – double delivery, single effect.
4. Billing guard – suspends PAST_DUE + zero balance.
5. Pre-paid credits – skip invoicer.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Dict

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Recharge,
    RechargeStatus,
    Users,
    WebhookLog,
)
from orchestra.lib.time import month_end_utc
from orchestra.observability.metrics import billing_suspended_total
from orchestra.routines import billing_guard as guard
from orchestra.routines import monthly_invoicer as invoicer
from orchestra.settings import settings


@contextlib.contextmanager
def _guard_uses(dbsession: Session):
    """Temporarily monkey-patch guard.SessionLocal to return the given session."""
    import orchestra.routines.billing_guard as guard

    orig = guard.SessionLocal
    guard.SessionLocal = lambda: dbsession
    try:
        yield
    finally:
        guard.SessionLocal = orig


@contextlib.contextmanager
def _routine_uses_session(module, dbsession):
    """Temporarily monkey-patch any module's SessionLocal to return the given session."""
    orig = module.SessionLocal
    module.SessionLocal = lambda: dbsession
    try:
        yield
    finally:
        module.SessionLocal = orig


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

    def _construct_event(payload, sig_header, secret):
        calls["construct"].append({"sig": sig_header})
        return json.loads(payload)

    dummy = SimpleNamespace(
        InvoiceItem=SimpleNamespace(create=_item_create),
        Invoice=SimpleNamespace(create=_inv_create),
        Webhook=SimpleNamespace(construct_event=_construct_event),
    )

    # Patch both the stripe_client module AND the monthly_invoicer's stripe import
    import orchestra.routines.monthly_invoicer as monthly_invoicer
    import orchestra.services.stripe_client as stripe_client

    monkeypatch.setattr(stripe_client, "stripe", dummy, raising=True)
    monkeypatch.setattr(monthly_invoicer, "stripe", dummy, raising=True)
    return calls


# --------------------------------------------------------------------------- #
# ensure settings have dummy secrets so pydantic doesn't explode              #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _env_secrets(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_SECRET_KEY_LIVE", "sk_test_dummy")
    # ensure the live settings instance has the field
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test", raising=False)


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
    ucols = {c["name"] for c in insp.get_columns("users")}

    assert {"status", "stripe_invoice_id", "invoice_group"} <= rcols
    assert "billing_state" in ucols


# --------------------------------------------------------------------------- #
# 1. Invoicer aggregates rows & flips                                         #
# --------------------------------------------------------------------------- #
def test_invoicer_aggregates(dbsession: Session, mock_stripe):
    uid = "user_inv"
    dbsession.add(Users(id=uid, credits=0, stripe_customer_id="cus_test"))

    for _ in range(3):
        dbsession.add(
            Recharge(
                user_id=uid,
                quantity=10,
                amount_usd=Decimal("2.50"),
                status=RechargeStatus.PENDING_INVOICE,
                invoice_group=LAST_GROUP,
                type="usage",
            ),
        )
    dbsession.commit()

    with _routine_uses_session(invoicer, dbsession):
        invoicer.invoice_month(LAST_MONTH_END.year, LAST_MONTH_END.month)

    rows = dbsession.query(Recharge).filter_by(user_id=uid).all()
    assert {r.status for r in rows} == {RechargeStatus.INVOICE_CREATED}
    assert {r.stripe_invoice_id for r in rows} == {"in_test_123"}
    assert len(mock_stripe["item"]) == 1 and len(mock_stripe["invoice"]) == 1


# --------------------------------------------------------------------------- #
# 2. Webhook idempotency (HTTP)                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_webhook_idempotent(client: AsyncClient, dbsession: Session):
    uid = "webhook_u"
    dbsession.add(Users(id=uid, stripe_customer_id="cus_x"))
    rec = Recharge(
        user_id=uid,
        quantity=5,
        amount_usd=Decimal("1.25"),
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
                "metadata": {"user_id": uid},
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
# 3. Billing guard                                                            #
# --------------------------------------------------------------------------- #
def test_guard_suspends(dbsession: Session):
    dbsession.add_all(
        [
            Users(id="off", billing_state="PAST_DUE", credits=0),
            Users(id="ok", billing_state="OK", credits=10),
        ],
    )
    dbsession.commit()

    start = billing_suspended_total._value.get()
    with _guard_uses(dbsession):
        guard.suspend_past_due_users()

    assert dbsession.get(Users, "off").billing_state == "SUSPENDED"
    assert dbsession.get(Users, "ok").billing_state == "OK"
    assert billing_suspended_total._value.get() == start + 1


# --------------------------------------------------------------------------- #
# 4. Pre-paid credit row must be skipped                                      #
# --------------------------------------------------------------------------- #
def test_prepaid_skip(dbsession: Session, mock_stripe):
    uid = "prepaid_u"
    dbsession.add(Users(id=uid, credits=100))
    rec = Recharge(
        user_id=uid,
        quantity=500,
        amount_usd=Decimal("5.00"),
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
