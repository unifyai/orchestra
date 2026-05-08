"""
Tests for the billing reconciliation routine.

Covers:
1.  Stale recharge detection (PENDING_INVOICE / INVOICE_CREATED)
2.  Auto-fix of stale recharges confirmed paid/void in Stripe
3.  Stripe customer health checks (deleted / missing customers)
4.  Orphaned Stripe invoices (paid invoices with no DB record)
5.  Credit balance integrity (autorecharge without customer, SUSPENDED flags)
6.  Stuck DISPUTED recharges with Stripe cross-reference
7.  Duplicate stripe_customer_id detection
8.  Payment method health for autorecharge accounts
9.  Credit balance ceiling sanity check
10. Webhook gap detection (Stripe events vs WebhookLog)
11. Admin endpoint (POST /v0/admin/billing/reconcile)
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus, WebhookLog
from orchestra.tests.test_billing.conftest import (
    make_billing_account,
    make_user,
    make_user_with_billing,
)


@pytest.fixture(autouse=True)
def _env_secrets(monkeypatch):
    import os

    from orchestra.settings import settings

    if not os.environ.get("STRIPE_SECRET_KEY"):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy_reconciliation")
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_reconciliation",
        raising=False,
    )


def _make_mock_stripe(
    *,
    invoice_retrieve=None,
    invoice_list=None,
    customer_retrieve=None,
    charge_retrieve=None,
    payment_method_list=None,
    event_list=None,
    InvalidRequestError=None,
    StripeError=None,
):
    """Build a complete mock stripe module for reconciliation tests."""
    return SimpleNamespace(
        api_key="sk_test_dummy",
        Invoice=SimpleNamespace(
            retrieve=invoice_retrieve or (lambda iid: {"id": iid, "status": "paid"}),
            list=invoice_list or (lambda **kw: _empty_stripe_list()),
        ),
        Customer=SimpleNamespace(
            retrieve=customer_retrieve or (lambda cid: SimpleNamespace(deleted=False)),
        ),
        Charge=SimpleNamespace(
            retrieve=charge_retrieve
            or (lambda cid: {"id": cid, "disputed": False, "dispute": None}),
        ),
        PaymentMethod=SimpleNamespace(
            list=payment_method_list or (lambda **kw: _empty_stripe_list()),
        ),
        Event=SimpleNamespace(
            list=event_list or (lambda **kw: _empty_stripe_list()),
        ),
        InvalidRequestError=InvalidRequestError or Exception,
        StripeError=StripeError or Exception,
    )


# ============================================================================
# Stale Recharges
# ============================================================================


class TestStaleRecharges:
    """Recharges stuck in PENDING_INVOICE or INVOICE_CREATED are flagged."""

    def test_invoice_created_but_stripe_says_paid(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """INVOICE_CREATED in DB + 'paid' in Stripe → critical discrepancy."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "paid"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_stale_1",
            credits=100,
            stripe_customer_id="cus_recon_1",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_stale_paid",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, stale_hours=48)

        stale = [
            d for d in result.discrepancies if d.category == "recharge_status_mismatch"
        ]
        assert len(stale) == 1
        assert stale[0].severity == "critical"
        assert "paid" in stale[0].detail

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.INVOICE_CREATED  # no auto_fix

    def test_auto_fix_invoice_created_paid(self, dbsession: Session, monkeypatch):
        """With auto_fix='moderate', INVOICE_CREATED → PAID when Stripe confirms."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "paid"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_fix_1",
            credits=100,
            stripe_customer_id="cus_recon_fix_1",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_fix_paid",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(
            session=dbsession,
            auto_fix="moderate",
            stale_hours=48,
        )

        assert result.auto_fixed_count >= 1
        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID

    def test_auto_fix_invoice_created_void(self, dbsession: Session, monkeypatch):
        """With auto_fix='all', INVOICE_CREATED → FAILED and credits voided
        when Stripe says the invoice is void."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "void"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_fix_void",
            credits=80,
            stripe_customer_id="cus_recon_fix_void",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("30"),
            amount_usd=Decimal("30"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_fix_void",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="all", stale_hours=48)

        assert result.auto_fixed_count >= 1
        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.FAILED
        dbsession.refresh(ba)
        assert float(ba.credits) == 50.0  # 80 - 30 voided

    def test_missing_stripe_invoice_is_critical(self, dbsession: Session, monkeypatch):
        """Recharge referencing a non-existent Stripe invoice → critical."""
        import orchestra.routines.billing_reconciliation as recon_mod

        class MockInvalidRequest(Exception):
            pass

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: (_ for _ in ()).throw(
                    MockInvalidRequest("No such invoice"),
                ),
                InvalidRequestError=MockInvalidRequest,
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_missing_inv",
            credits=100,
            stripe_customer_id="cus_recon_missing",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_does_not_exist",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, stale_hours=48)

        missing = [
            d for d in result.discrepancies if d.category == "missing_stripe_invoice"
        ]
        assert len(missing) == 1
        assert missing[0].severity == "critical"

    def test_fresh_recharges_not_flagged(self, dbsession: Session, monkeypatch):
        """Recharges younger than stale_hours are not checked."""
        import orchestra.routines.billing_reconciliation as recon_mod

        invoice_calls = []
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: invoice_calls.append(iid),
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_fresh",
            credits=100,
            stripe_customer_id="cus_recon_fresh",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_fresh",
            type="auto",
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, stale_hours=48)

        assert len(invoice_calls) == 0
        assert result.recharges_checked == 0


# ============================================================================
# Stripe Customer Health
# ============================================================================


class TestStripeCustomerHealth:
    """Billing accounts with deleted or missing Stripe customers."""

    def test_deleted_customer_flagged(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: SimpleNamespace(deleted=True),
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_del_cus",
            credits=100,
            stripe_customer_id="cus_deleted_recon",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        deleted = [
            d for d in result.discrepancies if d.category == "deleted_stripe_customer"
        ]
        assert len(deleted) == 1
        assert deleted[0].severity == "critical"

    def test_missing_customer_flagged(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        class MockInvalidRequest(Exception):
            pass

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: (_ for _ in ()).throw(
                    MockInvalidRequest("No such customer"),
                ),
                InvalidRequestError=MockInvalidRequest,
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_no_cus",
            credits=100,
            stripe_customer_id="cus_gone_recon",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        missing = [
            d for d in result.discrepancies if d.category == "missing_stripe_customer"
        ]
        assert len(missing) == 1
        assert missing[0].severity == "critical"

    def test_healthy_customer_no_discrepancy(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "recon_healthy",
            credits=100,
            stripe_customer_id="cus_healthy_recon",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        customer_issues = [
            d
            for d in result.discrepancies
            if d.category in ("deleted_stripe_customer", "missing_stripe_customer")
        ]
        assert len(customer_issues) == 0

    def test_auto_fix_deleted_customer(self, dbsession: Session, monkeypatch):
        """auto_fix='safe' clears stripe_customer_id and disables autorecharge."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: SimpleNamespace(deleted=True),
            ),
        )

        ba = make_billing_account(
            dbsession,
            credits=100,
            stripe_customer_id="cus_del_af",
            account_status="ACTIVE",
            autorecharge=True,
        )
        make_user(dbsession, "recon_del_af", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe")

        dbsession.refresh(ba)
        assert ba.stripe_customer_id is None
        assert ba.autorecharge is False
        assert result.auto_fixed_count >= 1

    def test_auto_fix_missing_customer(self, dbsession: Session, monkeypatch):
        """auto_fix='safe' clears stripe_customer_id when customer doesn't exist."""
        import orchestra.routines.billing_reconciliation as recon_mod

        class MockInvalidRequest(Exception):
            pass

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: (_ for _ in ()).throw(
                    MockInvalidRequest("No such customer"),
                ),
                InvalidRequestError=MockInvalidRequest,
            ),
        )

        ba = make_billing_account(
            dbsession,
            credits=50,
            stripe_customer_id="cus_miss_af",
            account_status="ACTIVE",
            autorecharge=True,
        )
        make_user(dbsession, "recon_miss_af", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe")

        dbsession.refresh(ba)
        assert ba.stripe_customer_id is None
        assert ba.autorecharge is False
        assert result.auto_fixed_count >= 1

    def test_suspended_accounts_not_checked(self, dbsession: Session, monkeypatch):
        """SUSPENDED accounts are not checked for customer health
        (they're already locked down)."""
        import orchestra.routines.billing_reconciliation as recon_mod

        retrieve_calls = []
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: retrieve_calls.append(cid)
                or SimpleNamespace(deleted=False),
            ),
        )

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_suspended_recon",
            account_status="SUSPENDED",
        )
        make_user(dbsession, "recon_suspended", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        assert "cus_suspended_recon" not in retrieve_calls


# ============================================================================
# Credit Balance Integrity
# ============================================================================


class TestCreditBalanceIntegrity:
    """Status/credit balance mismatches."""

    def test_active_negative_credits_not_flagged(self, dbsession: Session, monkeypatch):
        """ACTIVE + negative credits is normal (balance-based enforcement)."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=-10,
            account_status="ACTIVE",
            stripe_customer_id="cus_neg_active",
        )
        make_user(dbsession, "recon_neg_active", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        mismatches = [
            d
            for d in result.discrepancies
            if d.category == "status_credit_mismatch" and d.billing_account_id == ba.id
        ]
        assert len(mismatches) == 0

    def test_suspended_positive_credits_flagged(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=50,
            account_status="SUSPENDED",
            stripe_customer_id="cus_susp_pos",
        )
        make_user(dbsession, "recon_susp_pos", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        mismatches = [
            d for d in result.discrepancies if d.category == "status_credit_mismatch"
        ]
        assert any(
            "SUSPENDED" in d.detail and "positive" in d.detail for d in mismatches
        )

    def test_autorecharge_without_customer_flagged(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=100,
            account_status="ACTIVE",
            autorecharge=True,
            stripe_customer_id=None,
        )
        make_user(dbsession, "recon_ar_no_cus", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        ar = [
            d for d in result.discrepancies if d.category == "autorecharge_no_customer"
        ]
        assert len(ar) == 1

    def test_active_negative_stays_active(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """ACTIVE + negative credits stays ACTIVE (balance-based enforcement)."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=-10,
            account_status="ACTIVE",
            stripe_customer_id="cus_neg_af",
        )
        make_user(dbsession, "recon_neg_af", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="moderate")

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

    def test_auto_fix_autorecharge_no_customer(self, dbsession: Session, monkeypatch):
        """auto_fix='safe' disables autorecharge when there's no customer."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=100,
            account_status="ACTIVE",
            autorecharge=True,
            stripe_customer_id=None,
        )
        make_user(dbsession, "recon_ar_nc_af", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe")

        dbsession.refresh(ba)
        assert ba.autorecharge is False
        assert result.auto_fixed_count >= 1

    def test_clean_account_no_discrepancies(self, dbsession: Session, monkeypatch):
        """A healthy ACTIVE account with positive credits has no issues."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "recon_clean",
            credits=100,
            stripe_customer_id="cus_clean_recon",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        integrity = [
            d
            for d in result.discrepancies
            if d.category in ("status_credit_mismatch", "autorecharge_no_customer")
            and d.billing_account_id == ba.id
        ]
        assert len(integrity) == 0


# ============================================================================
# Orphaned Invoice Auto-Fix
# ============================================================================


class TestOrphanedInvoiceAutoFix:
    """auto_fix='all' creates missing Recharge + credits for paid invoices."""

    def test_auto_fix_creates_recharge_and_credits(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Orphaned paid invoice with our metadata → Recharge created, credits added."""
        import orchestra.routines.billing_reconciliation as recon_mod

        ba = make_billing_account(
            dbsession,
            credits=Decimal("10"),
            account_status="ACTIVE",
            stripe_customer_id="cus_orph_af",
        )
        make_user(dbsession, "recon_orph_af", ba)
        dbsession.commit()
        ba_id = ba.id

        mock_invoices = [
            {
                "id": "in_orphan_1",
                "status": "paid",
                "amount_paid": 2500,
                "customer": "cus_orph_af",
                "metadata": {"billing_account_id": str(ba_id)},
            },
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_invoices),
                ),
            ),
        )

        result = recon_mod.reconcile(session=dbsession, auto_fix="all")

        assert result.auto_fixed_count >= 1
        dbsession.refresh(ba)
        assert ba.credits == Decimal("35")  # 10 + 25

        recharge = (
            dbsession.query(Recharge).filter_by(stripe_invoice_id="in_orphan_1").first()
        )
        assert recharge is not None
        assert recharge.status == RechargeStatus.PAID.value
        assert recharge.quantity == Decimal("25")

    def test_auto_fix_unsuspends_account(self, dbsession: Session, monkeypatch):
        """If the account is SUSPENDED and gets credits from an orphaned invoice,
        it should be restored to ACTIVE."""
        import orchestra.routines.billing_reconciliation as recon_mod

        ba = make_billing_account(
            dbsession,
            credits=Decimal("0"),
            account_status="SUSPENDED",
            stripe_customer_id="cus_orph_susp",
        )
        make_user(dbsession, "recon_orph_susp", ba)
        dbsession.commit()
        ba_id = ba.id

        mock_invoices = [
            {
                "id": "in_orphan_susp",
                "status": "paid",
                "amount_paid": 5000,
                "customer": "cus_orph_susp",
                "metadata": {"billing_account_id": str(ba_id)},
            },
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_invoices),
                ),
            ),
        )

        result = recon_mod.reconcile(session=dbsession, auto_fix="all")

        dbsession.refresh(ba)
        assert ba.credits == Decimal("50")
        assert ba.account_status == "ACTIVE"

    def test_no_auto_fix_without_flag(self, dbsession: Session, monkeypatch):
        """Without auto_fix, orphaned invoices are flagged but not fixed."""
        import orchestra.routines.billing_reconciliation as recon_mod

        ba = make_billing_account(
            dbsession,
            credits=Decimal("10"),
            account_status="ACTIVE",
            stripe_customer_id="cus_orph_naf",
        )
        make_user(dbsession, "recon_orph_naf", ba)
        dbsession.commit()
        ba_id = ba.id

        mock_invoices = [
            {
                "id": "in_orphan_naf",
                "status": "paid",
                "amount_paid": 2500,
                "customer": "cus_orph_naf",
                "metadata": {"billing_account_id": str(ba_id)},
            },
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_invoices),
                ),
            ),
        )

        result = recon_mod.reconcile(session=dbsession, auto_fix=False)

        dbsession.refresh(ba)
        assert ba.credits == Decimal("10")  # unchanged
        orphan = [
            d for d in result.discrepancies if d.category == "orphaned_stripe_invoice"
        ]
        assert len(orphan) == 1
        assert orphan[0].auto_fixed is False


# ============================================================================
# Result Structure
# ============================================================================


class TestResultStructure:
    """Tests for ReconciliationResult serialization."""

    def test_to_dict_has_required_fields(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        result = recon_mod.reconcile(session=dbsession)
        d = result.to_dict()

        assert "started_at" in d
        assert "finished_at" in d
        assert "stripe_mode" in d
        assert d["stripe_mode"] == "test"
        assert "accounts_checked" in d
        assert "recharges_checked" in d
        assert "invoices_checked" in d
        assert "disputes_checked" in d
        assert "events_checked" in d
        assert "total_discrepancies" in d
        assert "critical" in d
        assert "warnings" in d
        assert "discrepancies" in d
        assert isinstance(d["discrepancies"], list)


# ============================================================================
# Admin Endpoint
# ============================================================================


class TestAdminEndpoint:
    """Tests for POST /v0/admin/billing/reconcile."""

    @pytest.mark.anyio
    async def test_endpoint_returns_200(
        self,
        client: AsyncClient,
        dbsession,
        monkeypatch,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        with patch(
            "orchestra.routines.billing_reconciliation.stripe",
        ) as mock_stripe:
            mock_stripe.api_key = "sk_test_dummy"
            mock_stripe.Invoice.list.return_value = _empty_stripe_list()
            mock_stripe.Customer.retrieve.return_value = SimpleNamespace(deleted=False)
            mock_stripe.InvalidRequestError = Exception
            mock_stripe.StripeError = Exception

            resp = await client.post(
                "/v0/admin/billing/reconcile",
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "stripe_mode" in body
        assert "total_discrepancies" in body

    @pytest.mark.anyio
    async def test_endpoint_accepts_params(
        self,
        client: AsyncClient,
        dbsession,
        monkeypatch,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        with patch(
            "orchestra.routines.billing_reconciliation.stripe",
        ) as mock_stripe:
            mock_stripe.api_key = "sk_test_dummy"
            mock_stripe.Invoice.list.return_value = _empty_stripe_list()
            mock_stripe.Customer.retrieve.return_value = SimpleNamespace(deleted=False)
            mock_stripe.InvalidRequestError = Exception
            mock_stripe.StripeError = Exception

            resp = await client.post(
                "/v0/admin/billing/reconcile?auto_fix=false&lookback_days=7&stale_hours=24",
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == 200


# ============================================================================
# Stuck Disputed Recharges
# ============================================================================


class TestStuckDisputes:
    """DISPUTED recharges are cross-referenced with Stripe."""

    def test_dispute_won_flagged_critical(self, dbsession: Session, monkeypatch):
        """DISPUTED in DB + dispute won in Stripe → critical."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {
                    "id": iid,
                    "status": "paid",
                    "charge": "ch_won",
                },
                charge_retrieve=lambda cid: {
                    "id": cid,
                    "disputed": True,
                    "dispute": {"status": "won"},
                },
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_disp_won",
            credits=0,
            stripe_customer_id="cus_disp_won",
            account_status="SUSPENDED",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_disp_won",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, stale_hours=48)

        resolved = [
            d for d in result.discrepancies if d.category == "stuck_dispute_resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0].severity == "critical"
        assert "won" in resolved[0].detail

    def test_auto_fix_dispute_won(self, dbsession: Session, monkeypatch):
        """auto_fix='moderate' re-credits and sets DISPUTED → PAID for won disputes."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {
                    "id": iid,
                    "status": "paid",
                    "charge": "ch_fix_won",
                },
                charge_retrieve=lambda cid: {
                    "id": cid,
                    "disputed": True,
                    "dispute": {"status": "won"},
                },
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_fix_disp_won",
            credits=0,
            stripe_customer_id="cus_fix_disp_won",
            account_status="SUSPENDED",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_fix_disp_won",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(
            session=dbsession,
            auto_fix="moderate",
            stale_hours=48,
        )

        assert result.auto_fixed_count >= 1
        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID
        dbsession.refresh(ba)
        assert float(ba.credits) == 50.0
        assert ba.account_status == "ACTIVE"

    def test_dispute_lost_flagged_warning(self, dbsession: Session, monkeypatch):
        """DISPUTED in DB + dispute lost in Stripe → warning."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {
                    "id": iid,
                    "status": "paid",
                    "charge": "ch_lost",
                },
                charge_retrieve=lambda cid: {
                    "id": cid,
                    "disputed": True,
                    "dispute": {"status": "lost"},
                },
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_disp_lost",
            credits=0,
            stripe_customer_id="cus_disp_lost",
            account_status="SUSPENDED",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_disp_lost",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, stale_hours=48)

        resolved = [
            d for d in result.discrepancies if d.category == "stuck_dispute_resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0].severity == "warning"
        assert "lost" in resolved[0].detail

    def test_active_dispute_not_flagged(self, dbsession: Session, monkeypatch):
        """Disputes still under_review are not flagged as resolved."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {
                    "id": iid,
                    "status": "paid",
                    "charge": "ch_active",
                },
                charge_retrieve=lambda cid: {
                    "id": cid,
                    "disputed": True,
                    "dispute": {"status": "needs_response"},
                },
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_disp_active",
            credits=0,
            stripe_customer_id="cus_disp_active",
            account_status="SUSPENDED",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_disp_active",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, stale_hours=48)

        resolved = [
            d for d in result.discrepancies if d.category == "stuck_dispute_resolved"
        ]
        assert len(resolved) == 0


# ============================================================================
# Duplicate Stripe Customers
# ============================================================================


class TestDuplicateStripeCustomers:
    """Multiple billing accounts with the same stripe_customer_id.

    The DB has a unique constraint on stripe_customer_id, so duplicates
    shouldn't occur in normal operation.  This check is defense-in-depth
    for data corruption via raw SQL, migrations, or constraint removal.
    """

    def test_unique_customers_no_flag(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user1, _ = make_user_with_billing(
            dbsession,
            "recon_uniq_1",
            credits=100,
            stripe_customer_id="cus_unique_a",
        )
        user2, _ = make_user_with_billing(
            dbsession,
            "recon_uniq_2",
            credits=100,
            stripe_customer_id="cus_unique_b",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        dupes = [
            d for d in result.discrepancies if d.category == "duplicate_stripe_customer"
        ]
        assert len(dupes) == 0

    def test_null_customer_ids_not_flagged(self, dbsession: Session, monkeypatch):
        """Accounts without a stripe_customer_id should not be compared."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba1 = make_billing_account(dbsession, credits=100, stripe_customer_id=None)
        make_user(dbsession, "recon_null_cus_1", ba1)
        ba2 = make_billing_account(dbsession, credits=50, stripe_customer_id=None)
        make_user(dbsession, "recon_null_cus_2", ba2)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        dupes = [
            d for d in result.discrepancies if d.category == "duplicate_stripe_customer"
        ]
        assert len(dupes) == 0


# ============================================================================
# Payment Method Health
# ============================================================================


class TestPaymentMethods:
    """Autorecharge accounts need at least one payment method."""

    def test_no_payment_method_flagged(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                payment_method_list=lambda **kw: SimpleNamespace(data=[]),
            ),
        )

        ba = make_billing_account(
            dbsession,
            credits=100,
            account_status="ACTIVE",
            stripe_customer_id="cus_no_pm",
            autorecharge=True,
        )
        make_user(dbsession, "recon_no_pm", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        pm = [d for d in result.discrepancies if d.category == "missing_payment_method"]
        assert len(pm) == 1
        assert pm[0].severity == "warning"

    def test_has_payment_method_no_flag(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                payment_method_list=lambda **kw: SimpleNamespace(
                    data=[{"id": "pm_123", "type": "card"}],
                ),
            ),
        )

        ba = make_billing_account(
            dbsession,
            credits=100,
            account_status="ACTIVE",
            stripe_customer_id="cus_has_pm",
            autorecharge=True,
        )
        make_user(dbsession, "recon_has_pm", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        pm = [
            d
            for d in result.discrepancies
            if d.category == "missing_payment_method" and d.billing_account_id == ba.id
        ]
        assert len(pm) == 0

    def test_auto_fix_missing_pm_disables_autorecharge(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """auto_fix='safe' disables autorecharge when no payment methods."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                payment_method_list=lambda **kw: SimpleNamespace(data=[]),
            ),
        )

        ba = make_billing_account(
            dbsession,
            credits=100,
            account_status="ACTIVE",
            stripe_customer_id="cus_no_pm_af",
            autorecharge=True,
        )
        make_user(dbsession, "recon_no_pm_af", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe")

        dbsession.refresh(ba)
        assert ba.autorecharge is False
        assert result.auto_fixed_count >= 1

    def test_no_autorecharge_not_checked(self, dbsession: Session, monkeypatch):
        """Accounts without autorecharge aren't checked for payment methods."""
        import orchestra.routines.billing_reconciliation as recon_mod

        pm_calls = []
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                payment_method_list=lambda **kw: pm_calls.append(1)
                or SimpleNamespace(data=[]),
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "recon_no_ar_pm",
            credits=100,
            stripe_customer_id="cus_no_ar_pm",
            autorecharge=False,
        )
        dbsession.commit()

        recon_mod.reconcile(session=dbsession)

        assert len(pm_calls) == 0


# ============================================================================
# Credit Balance Ceiling
# ============================================================================


class TestCreditBalanceCeiling:
    """Credits must not exceed total PAID recharges."""

    def test_credits_exceed_recharges_flagged(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "recon_ceiling",
            credits=200,
            stripe_customer_id="cus_ceiling",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("100"),
            amount_usd=Decimal("100"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_ceiling",
            type="payment",
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        ceiling = [
            d
            for d in result.discrepancies
            if d.category == "credit_exceeds_recharged"
            and d.billing_account_id == ba.id
        ]
        assert len(ceiling) == 1
        assert ceiling[0].severity == "warning"
        assert "200" in ceiling[0].detail
        assert "100" in ceiling[0].detail

    def test_credits_within_ceiling_no_flag(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "recon_ok_ceil",
            credits=50,
            stripe_customer_id="cus_ok_ceil",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("100"),
            amount_usd=Decimal("100"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_ok_ceil",
            type="payment",
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        ceiling = [
            d
            for d in result.discrepancies
            if d.category == "credit_exceeds_recharged"
            and d.billing_account_id == ba.id
        ]
        assert len(ceiling) == 0

    def test_no_recharges_not_flagged(self, dbsession: Session, monkeypatch):
        """Accounts with credits but zero recharges are skipped (could be
        seed data or admin-granted credits)."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "recon_no_rech",
            credits=100,
            stripe_customer_id="cus_no_rech",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        ceiling = [
            d
            for d in result.discrepancies
            if d.category == "credit_exceeds_recharged"
            and d.billing_account_id == ba.id
        ]
        assert len(ceiling) == 0


# ============================================================================
# Webhook Gap Detection
# ============================================================================


class TestWebhookGaps:
    """Stripe events not in WebhookLog are flagged."""

    def test_missed_webhook_flagged(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod

        mock_events = [
            {"id": "evt_gap_1", "type": "invoice.payment_succeeded"},
            {"id": "evt_gap_2", "type": "charge.dispute.created"},
            {"id": "evt_gap_3", "type": "customer.updated"},  # not billing-relevant
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                event_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_events),
                ),
            ),
        )

        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        missed = [d for d in result.discrepancies if d.category == "missed_webhook"]
        missed_ids = {d.stripe_id for d in missed}
        assert "evt_gap_1" in missed_ids
        assert "evt_gap_2" in missed_ids
        assert (
            "evt_gap_3" not in missed_ids
        )  # customer.updated not in BILLING_EVENT_TYPES

    def test_logged_webhook_not_flagged(self, dbsession: Session, monkeypatch):
        """Events already in WebhookLog are not flagged."""
        import uuid

        import orchestra.routines.billing_reconciliation as recon_mod

        mock_events = [
            {"id": "evt_ok_1", "type": "invoice.payment_succeeded"},
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                event_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_events),
                ),
            ),
        )

        dbsession.add(
            WebhookLog(
                id=str(uuid.uuid4()),
                event_id="evt_ok_1",
                event_type="invoice.payment_succeeded",
            ),
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        missed = [
            d
            for d in result.discrepancies
            if d.category == "missed_webhook" and d.stripe_id == "evt_ok_1"
        ]
        assert len(missed) == 0

    def test_auto_fix_replays_missed_event(self, dbsession: Session, monkeypatch):
        """auto_fix='all' replays missed events through handle_event."""
        import orchestra.routines.billing_reconciliation as recon_mod

        mock_events = [
            {
                "id": "evt_replay_1",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"id": "in_replay"}},
            },
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                event_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_events),
                ),
            ),
        )

        replayed = []

        def _mock_handle_event(event_dict):
            replayed.append(event_dict)
            return SimpleNamespace(status_code=200)

        with patch(
            "orchestra.web.api.webhooks.stripe.handle_event",
            _mock_handle_event,
        ):
            result = recon_mod.reconcile(session=dbsession, auto_fix="all")

        assert len(replayed) == 1
        assert replayed[0]["id"] == "evt_replay_1"
        missed = [d for d in result.discrepancies if d.category == "missed_webhook"]
        assert len(missed) == 1
        assert missed[0].auto_fixed is True

    def test_auto_fix_replay_failure_recorded(self, dbsession: Session, monkeypatch):
        """If replay fails, the error is recorded but reconciliation continues."""
        import orchestra.routines.billing_reconciliation as recon_mod

        mock_events = [
            {
                "id": "evt_replay_fail",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"id": "in_fail"}},
            },
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                event_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_events),
                ),
            ),
        )

        def _mock_handle_event(event_dict):
            raise RuntimeError("Handler crashed")

        with patch(
            "orchestra.web.api.webhooks.stripe.handle_event",
            _mock_handle_event,
        ):
            result = recon_mod.reconcile(session=dbsession, auto_fix="all")

        missed = [d for d in result.discrepancies if d.category == "missed_webhook"]
        assert len(missed) == 1
        assert missed[0].auto_fixed is False
        assert any("Failed to replay" in e for e in result.errors)

    def test_event_list_failure_graceful(self, dbsession: Session, monkeypatch):
        """If listing Stripe events fails, an error is recorded but the
        reconciliation still completes."""
        import orchestra.routines.billing_reconciliation as recon_mod

        def _fail(**kw):
            raise RuntimeError("Stripe API down")

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                event_list=_fail,
            ),
        )

        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        assert any("Failed to list Stripe events" in e for e in result.errors)
        assert result.events_checked == 0


# ============================================================================
# Enrichment (owner identity, Stripe URLs, recharge context)
# ============================================================================


class TestEnrichment:
    """Post-collection enrichment of discrepancies with owner info,
    Stripe dashboard links, and recent recharge context."""

    def test_owner_lookup_user(self, dbsession: Session, monkeypatch):
        """Discrepancy for a user-owned BA gets owner_type='user' + email."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: SimpleNamespace(deleted=True),
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "enrich_user_1",
            email="owner@example.com",
            credits=10,
            stripe_customer_id="cus_enrich_u1",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        cust_disc = [
            d
            for d in result.discrepancies
            if d.billing_account_id == ba.id and d.stripe_id == "cus_enrich_u1"
        ]
        assert len(cust_disc) >= 1
        d = cust_disc[0]
        assert d.owner_type == "user"
        assert d.owner_email == "owner@example.com"

    def test_owner_lookup_org(self, dbsession: Session, monkeypatch):
        """Discrepancy for an org-owned BA gets owner_type='org' + org name."""
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.tests.test_billing.conftest import (
            make_billing_account,
            make_org,
            make_user,
        )

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: SimpleNamespace(deleted=True),
            ),
        )

        owner_ba = make_billing_account(dbsession, credits=100)
        owner = make_user(
            dbsession,
            "enrich_org_owner",
            owner_ba,
            email="orgowner@co.com",
        )

        org_ba = make_billing_account(
            dbsession,
            credits=10,
            stripe_customer_id="cus_enrich_org1",
        )
        org = make_org(dbsession, owner, org_ba, name="EnrichTestOrg")
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        org_disc = [
            d
            for d in result.discrepancies
            if d.billing_account_id == org_ba.id and d.stripe_id == "cus_enrich_org1"
        ]
        assert len(org_disc) >= 1
        d = org_disc[0]
        assert d.owner_type == "org"
        assert d.owner_name == "EnrichTestOrg"
        assert d.owner_email == "orgowner@co.com"

    def test_stripe_dashboard_url(self, dbsession: Session, monkeypatch):
        """Discrepancies with a stripe_id get a Stripe Dashboard URL."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: SimpleNamespace(deleted=True),
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "enrich_url_1",
            credits=10,
            stripe_customer_id="cus_enrich_url1",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        cust_disc = [
            d for d in result.discrepancies if d.stripe_id == "cus_enrich_url1"
        ]
        assert len(cust_disc) >= 1
        d = cust_disc[0]
        assert d.stripe_url is not None
        assert "cus_enrich_url1" in d.stripe_url
        assert "/test/" in d.stripe_url  # test mode key → /test/ prefix

    def test_recharge_context_for_credit_discrepancy(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Credit-related discrepancies include recent recharge history."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "enrich_ctx_1",
            credits=50,
            account_status="SUSPENDED",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("25"),
            amount_usd=Decimal("25"),
            status=RechargeStatus.PAID,
            type="payment",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2),
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        credit_disc = [
            d
            for d in result.discrepancies
            if d.billing_account_id == ba.id and d.category == "status_credit_mismatch"
        ]
        assert len(credit_disc) >= 1
        d = credit_disc[0]
        assert d.recharge_context is not None
        assert len(d.recharge_context) >= 1
        assert d.recharge_context[0]["amount_usd"] == 25.0

    def test_recharge_context_capped_at_five(self, dbsession: Session, monkeypatch):
        """Only the 5 most recent recharges are included."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "enrich_cap_1",
            credits=100,
            account_status="SUSPENDED",
        )
        now = _dt.datetime.now(_dt.timezone.utc)
        for i in range(8):
            dbsession.add(
                Recharge(
                    billing_account_id=ba.id,
                    quantity=Decimal("10"),
                    amount_usd=Decimal("10"),
                    status=RechargeStatus.PAID,
                    type="payment",
                    at=now - _dt.timedelta(hours=i),
                ),
            )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        credit_disc = [
            d
            for d in result.discrepancies
            if d.billing_account_id == ba.id and d.category == "status_credit_mismatch"
        ]
        assert len(credit_disc) >= 1
        assert len(credit_disc[0].recharge_context) == 5

    def test_to_dict_includes_enrichment_fields(self, dbsession: Session, monkeypatch):
        """ReconciliationResult.to_dict() includes the enrichment fields."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                customer_retrieve=lambda cid: SimpleNamespace(deleted=True),
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "enrich_dict_1",
            email="serial@test.com",
            credits=10,
            stripe_customer_id="cus_enrich_dict1",
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)
        d = result.to_dict()

        enriched = [
            disc
            for disc in d["discrepancies"]
            if disc.get("stripe_id") == "cus_enrich_dict1"
        ]
        assert len(enriched) >= 1
        disc = enriched[0]
        assert "owner_type" in disc
        assert "owner_email" in disc
        assert "owner_name" in disc
        assert "stripe_url" in disc
        assert "recharge_context" in disc

    def test_stripe_url_live_mode(self):
        """Live-mode Stripe URLs don't include /test/ prefix."""
        from orchestra.routines.billing_reconciliation import _stripe_dashboard_url

        url = _stripe_dashboard_url("cus_abc123", "live")
        assert url == "https://dashboard.stripe.com/customers/cus_abc123"

    def test_stripe_url_test_mode(self):
        """Test-mode Stripe URLs include /test/ prefix."""
        from orchestra.routines.billing_reconciliation import _stripe_dashboard_url

        url = _stripe_dashboard_url("in_abc123", "test")
        assert url == "https://dashboard.stripe.com/test/invoices/in_abc123"

    def test_stripe_url_unknown_prefix(self):
        """Unknown Stripe ID prefixes return None."""
        from orchestra.routines.billing_reconciliation import _stripe_dashboard_url

        assert _stripe_dashboard_url("unknown_123", "live") is None

    def test_stripe_url_none_id(self):
        """None stripe_id returns None."""
        from orchestra.routines.billing_reconciliation import _stripe_dashboard_url

        assert _stripe_dashboard_url(None, "live") is None


# ============================================================================
# Fix Tier Boundaries
# ============================================================================


class TestFixTierBoundaries:
    """Verify that each tier only applies its own fixes and below."""

    def test_safe_fixes_autorecharge_no_customer(self, dbsession: Session, monkeypatch):
        """auto_fix='safe' SHOULD disable autorecharge without a customer."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=100,
            account_status="ACTIVE",
            autorecharge=True,
            stripe_customer_id=None,
        )
        make_user(dbsession, "tier_s2", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe")

        dbsession.refresh(ba)
        assert ba.autorecharge is False
        assert result.auto_fixed_count >= 1

    def test_moderate_does_not_fix_void_credits(self, dbsession: Session, monkeypatch):
        """auto_fix='moderate' should NOT void credits for void invoices (requires 'all')."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "void"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "tier_m_void",
            credits=80,
            stripe_customer_id="cus_tier_m_void",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("30"),
            amount_usd=Decimal("30"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_tier_m_void",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(
            session=dbsession,
            auto_fix="moderate",
            stale_hours=48,
        )

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.INVOICE_CREATED
        dbsession.refresh(ba)
        assert float(ba.credits) == 80.0
        void_disc = [
            d
            for d in result.discrepancies
            if d.stripe_id == "in_tier_m_void" and "void" in d.detail
        ]
        assert len(void_disc) == 1
        assert void_disc[0].auto_fixed is False

    def test_moderate_fixes_paid_recharge(self, dbsession: Session, monkeypatch):
        """auto_fix='moderate' SHOULD fix INVOICE_CREATED → PAID when Stripe confirms."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "paid"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "tier_m_paid",
            credits=100,
            stripe_customer_id="cus_tier_m_paid",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_tier_m_paid",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(
            session=dbsession,
            auto_fix="moderate",
            stale_hours=48,
        )

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID

    def test_moderate_does_not_replay_webhooks(self, dbsession: Session, monkeypatch):
        """auto_fix='moderate' should NOT replay missed webhooks (requires 'all')."""
        import orchestra.routines.billing_reconciliation as recon_mod

        mock_events = [
            {
                "id": "evt_tier_no_replay",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"id": "in_noreplay"}},
            },
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                event_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_events),
                ),
            ),
        )

        replayed = []

        def _mock_handle_event(event_dict):
            replayed.append(event_dict)
            return SimpleNamespace(status_code=200)

        with patch(
            "orchestra.web.api.webhooks.stripe.handle_event",
            _mock_handle_event,
        ):
            result = recon_mod.reconcile(session=dbsession, auto_fix="moderate")

        assert len(replayed) == 0
        missed = [d for d in result.discrepancies if d.category == "missed_webhook"]
        assert len(missed) == 1
        assert missed[0].auto_fixed is False

    def test_moderate_does_not_fix_orphaned_invoices(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """auto_fix='moderate' should NOT create recharges for orphaned invoices."""
        import orchestra.routines.billing_reconciliation as recon_mod

        ba = make_billing_account(
            dbsession,
            credits=Decimal("10"),
            account_status="ACTIVE",
            stripe_customer_id="cus_tier_m_orph",
        )
        make_user(dbsession, "tier_m_orph", ba)
        dbsession.commit()
        ba_id = ba.id

        mock_invoices = [
            {
                "id": "in_tier_orph",
                "status": "paid",
                "amount_paid": 2500,
                "customer": "cus_tier_m_orph",
                "metadata": {"billing_account_id": str(ba_id)},
            },
        ]
        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_list=lambda **kw: SimpleNamespace(
                    auto_paging_iter=lambda: iter(mock_invoices),
                ),
            ),
        )

        result = recon_mod.reconcile(session=dbsession, auto_fix="moderate")

        dbsession.refresh(ba)
        assert ba.credits == Decimal("10")
        orphan = [
            d for d in result.discrepancies if d.category == "orphaned_stripe_invoice"
        ]
        assert len(orphan) == 1
        assert orphan[0].auto_fixed is False

    def test_backward_compat_bool_true_maps_to_all(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Passing auto_fix=True (bool) maps to 'all' tier."""
        from orchestra.routines.billing_reconciliation import FIX_ALL, _parse_fix_level

        assert _parse_fix_level(True) == FIX_ALL

    def test_backward_compat_bool_false_maps_to_none(self):
        """Passing auto_fix=False (bool) maps to 'none' tier."""
        from orchestra.routines.billing_reconciliation import FIX_NONE, _parse_fix_level

        assert _parse_fix_level(False) == FIX_NONE

    def test_parse_fix_level_unknown_string(self):
        """Unknown tier string defaults to 'none'."""
        from orchestra.routines.billing_reconciliation import FIX_NONE, _parse_fix_level

        assert _parse_fix_level("unknown") == FIX_NONE
        assert _parse_fix_level("") == FIX_NONE

    def test_safe_dispute_lost_is_fixed(self, dbsession: Session, monkeypatch):
        """auto_fix='safe' should fix dispute lost → FAILED (no credit impact)."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {
                    "id": iid,
                    "status": "paid",
                    "charge": "ch_safe_lost",
                },
                charge_retrieve=lambda cid: {
                    "id": cid,
                    "disputed": True,
                    "dispute": {"status": "lost"},
                },
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "tier_safe_lost",
            credits=0,
            stripe_customer_id="cus_tier_safe_lost",
            account_status="SUSPENDED",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_tier_safe_lost",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe", stale_hours=48)

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.FAILED
        resolved = [
            d for d in result.discrepancies if d.category == "stuck_dispute_resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0].auto_fixed is True

    def test_safe_dispute_won_not_fixed(self, dbsession: Session, monkeypatch):
        """auto_fix='safe' should NOT fix dispute won (requires moderate for credit restore)."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {
                    "id": iid,
                    "status": "paid",
                    "charge": "ch_safe_won",
                },
                charge_retrieve=lambda cid: {
                    "id": cid,
                    "disputed": True,
                    "dispute": {"status": "won"},
                },
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "tier_safe_won",
            credits=0,
            stripe_customer_id="cus_tier_safe_won",
            account_status="SUSPENDED",
        )
        old_time = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=72)
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_tier_safe_won",
            type="auto",
            at=old_time,
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe", stale_hours=48)

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.DISPUTED
        resolved = [
            d for d in result.discrepancies if d.category == "stuck_dispute_resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0].auto_fixed is False


# ============================================================================
# Failed Recharge Credit Void Verification
# ============================================================================


class TestFailedRechargeVoids:
    """FAILED auto-recharges should have their credits voided."""

    def test_unvoided_failed_recharge_flagged(self, dbsession: Session, monkeypatch):
        """FAILED auto-recharge with unvoided credits is flagged as critical."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "void"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "void_check_1",
            credits=100,
            stripe_customer_id="cus_void1",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.FAILED,
            stripe_invoice_id="in_void_check",
            type="auto",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48),
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        unvoided = [
            d
            for d in result.discrepancies
            if d.category == "unvoided_failed_recharge"
            and d.billing_account_id == ba.id
        ]
        assert len(unvoided) == 1
        assert unvoided[0].severity == "critical"
        assert not unvoided[0].auto_fixed

    def test_voided_failed_recharge_not_flagged(self, dbsession: Session, monkeypatch):
        """FAILED recharge where credits were properly voided is clean."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "void"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "void_check_2",
            credits=0,
            stripe_customer_id="cus_void2",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.FAILED,
            stripe_invoice_id="in_void_clean",
            type="auto",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48),
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        unvoided = [
            d
            for d in result.discrepancies
            if d.category == "unvoided_failed_recharge"
            and d.billing_account_id == ba.id
        ]
        assert len(unvoided) == 0

    def test_auto_fix_deducts_unvoided_credits(self, dbsession: Session, monkeypatch):
        """auto_fix='all' deducts the unvoided credits."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(
            recon_mod,
            "stripe",
            _make_mock_stripe(
                invoice_retrieve=lambda iid: {"id": iid, "status": "void"},
            ),
        )

        user, ba = make_user_with_billing(
            dbsession,
            "void_fix_1",
            credits=75,
            stripe_customer_id="cus_void_fix",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.FAILED,
            stripe_invoice_id="in_void_fix",
            type="auto",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48),
        )
        dbsession.add(rec)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="all")

        dbsession.refresh(ba)
        assert ba.credits == Decimal("25")
        unvoided = [
            d
            for d in result.discrepancies
            if d.category == "unvoided_failed_recharge"
            and d.billing_account_id == ba.id
        ]
        assert len(unvoided) == 1
        assert unvoided[0].auto_fixed is True


# ============================================================================
# Orphaned Grace Periods
# ============================================================================


class TestOrphanedGracePeriods:
    """Contacts in grace_period with non-negative BA credits are flagged."""

    def test_grace_period_with_positive_credits_flagged(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.models.orchestra_models import Assistant, AssistantContact

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "grace_orphan_1",
            credits=50,
            stripe_customer_id="cus_grace_orphan",
        )
        assistant = Assistant(
            user_id=user.id,
            first_name="TestAssistant",
        )
        dbsession.add(assistant)
        dbsession.flush()

        contact = AssistantContact(
            assistant_id=assistant.agent_id,
            contact_type="phone",
            contact_value="+15551234567",
            provider="twilio",
            provisioned_by="platform",
            status="grace_period",
            grace_period_started_at=_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=5),
        )
        dbsession.add(contact)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        orphaned = [
            d
            for d in result.discrepancies
            if d.category == "orphaned_grace_period" and d.billing_account_id == ba.id
        ]
        assert len(orphaned) == 1
        assert orphaned[0].severity == "warning"

    def test_grace_period_with_negative_credits_not_flagged(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.models.orchestra_models import Assistant, AssistantContact

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "grace_ok_1",
            credits=-10,
            stripe_customer_id="cus_grace_ok",
        )
        assistant = Assistant(
            user_id=user.id,
            first_name="TestAssistant2",
        )
        dbsession.add(assistant)
        dbsession.flush()

        contact = AssistantContact(
            assistant_id=assistant.agent_id,
            contact_type="phone",
            contact_value="+15559876543",
            provider="twilio",
            provisioned_by="platform",
            status="grace_period",
            grace_period_started_at=_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=3),
        )
        dbsession.add(contact)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        orphaned = [
            d
            for d in result.discrepancies
            if d.category == "orphaned_grace_period" and d.billing_account_id == ba.id
        ]
        assert len(orphaned) == 0

    def test_auto_fix_safe_restores_contact(self, dbsession: Session, monkeypatch):
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.models.orchestra_models import Assistant, AssistantContact

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        user, ba = make_user_with_billing(
            dbsession,
            "grace_fix_1",
            credits=100,
            stripe_customer_id="cus_grace_fix",
        )
        assistant = Assistant(
            user_id=user.id,
            first_name="TestAssistant3",
        )
        dbsession.add(assistant)
        dbsession.flush()

        contact = AssistantContact(
            assistant_id=assistant.agent_id,
            contact_type="email",
            contact_value="test@example.com",
            provider="google_workspace",
            provisioned_by="platform",
            status="grace_period",
            grace_period_started_at=_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=2),
        )
        dbsession.add(contact)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe")

        dbsession.refresh(contact)
        assert contact.status == "active"
        assert contact.grace_period_started_at is None

        orphaned = [
            d
            for d in result.discrepancies
            if d.category == "orphaned_grace_period" and d.billing_account_id == ba.id
        ]
        assert len(orphaned) == 1
        assert orphaned[0].auto_fixed is True


# ============================================================================
# Unjustified Suspensions
# ============================================================================


class TestUnjustifiedSuspensions:
    """SUSPENDED accounts without justified reason are flagged."""

    def test_suspended_no_reason_flagged(self, dbsession: Session, monkeypatch):
        """SUSPENDED with no suspension_reason is flagged as warning."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=0,
            account_status="SUSPENDED",
            stripe_customer_id="cus_unjust_1",
        )
        make_user(dbsession, "unjust_1", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        unjust = [
            d
            for d in result.discrepancies
            if d.category == "unjustified_suspension" and d.billing_account_id == ba.id
        ]
        assert len(unjust) == 1
        assert unjust[0].severity == "warning"
        assert "no suspension reason" in unjust[0].detail

    def test_suspended_dispute_reason_no_active_disputes_flagged(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """SUSPENDED with reason='dispute' but no active disputes is flagged."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=0,
            account_status="SUSPENDED",
            stripe_customer_id="cus_unjust_disp",
        )
        ba.suspension_reason = "dispute"
        make_user(dbsession, "unjust_disp_1", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        unjust = [
            d
            for d in result.discrepancies
            if d.category == "unjustified_suspension" and d.billing_account_id == ba.id
        ]
        assert len(unjust) == 1
        assert unjust[0].severity == "info"

    def test_suspended_with_active_dispute_not_flagged(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=0,
            account_status="SUSPENDED",
            stripe_customer_id="cus_just_1",
        )
        ba.suspension_reason = "dispute"
        make_user(dbsession, "just_1", ba)
        disputed_recharge = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_disputed",
            type="auto",
            at=_dt.datetime.now(_dt.timezone.utc),
        )
        dbsession.add(disputed_recharge)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)

        unjust = [
            d
            for d in result.discrepancies
            if d.category == "unjustified_suspension" and d.billing_account_id == ba.id
        ]
        assert len(unjust) == 0

    def test_admin_freeze_always_skipped(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """SUSPENDED with reason='admin_freeze' is never flagged."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=100,
            account_status="SUSPENDED",
            stripe_customer_id="cus_admin_frz",
        )
        ba.suspension_reason = "admin_freeze"
        make_user(dbsession, "admin_frz_1", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="all")

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == "admin_freeze"
        unjust = [
            d
            for d in result.discrepancies
            if d.category == "unjustified_suspension" and d.billing_account_id == ba.id
        ]
        assert len(unjust) == 0

    def test_auto_fix_moderate_restores_no_reason(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """moderate auto-fix restores SUSPENDED with no reason."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=25,
            account_status="SUSPENDED",
            stripe_customer_id="cus_unjust_fix",
        )
        make_user(dbsession, "unjust_fix_1", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="moderate")

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None
        unjust = [
            d
            for d in result.discrepancies
            if d.category == "unjustified_suspension" and d.billing_account_id == ba.id
        ]
        assert len(unjust) == 1
        assert unjust[0].auto_fixed is True

    def test_auto_fix_moderate_restores_dispute_no_active(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """moderate auto-fix restores SUSPENDED with reason='dispute' when
        no active DISPUTED recharges remain."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=10,
            account_status="SUSPENDED",
            stripe_customer_id="cus_unjust_disp_fix",
        )
        ba.suspension_reason = "dispute"
        make_user(dbsession, "unjust_disp_fix", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="moderate")

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None

    def test_safe_does_not_fix_unjustified_suspension(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """auto_fix='safe' should NOT restore SUSPENDED accounts."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=50,
            account_status="SUSPENDED",
            stripe_customer_id="cus_unjust_safe",
        )
        make_user(dbsession, "unjust_safe_1", ba)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, auto_fix="safe")

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"
        unjust = [
            d
            for d in result.discrepancies
            if d.category == "unjustified_suspension" and d.billing_account_id == ba.id
        ]
        assert len(unjust) >= 1
        assert unjust[0].auto_fixed is False


# ============================================================================
# Managed-billing v2 — plan assignment integrity
# ============================================================================


def _make_metered_template(dbsession, *, name: str):
    """Cheap METERED template for reconciliation tests."""
    from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
    from orchestra.db.models.enums import (
        BillingMode,
        CollectionMethod,
    )

    return BillingPlanTemplateDAO(dbsession).create_template(
        name=name,
        billing_mode=BillingMode.METERED,
        commit_amount=Decimal("1000"),
        commit_period="MONTHLY",
        base_pricing_factor=Decimal("1.0"),
        overage_pricing_factor=Decimal("1.0"),
        collection_method=CollectionMethod.SEND_INVOICE_NET_30,
        is_custom=True,
        is_active=True,
    )


class TestPlanAssignmentIntegrity:
    """``_check_plan_assignment_integrity`` — denormalised pointer health."""

    def test_null_pointer_is_critical(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """A BA whose plan_assignment_id is NULL violates the v2 invariant."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        # `make_billing_account` routes through BillingAccountDAO.create,
        # which inserts a default plan assignment. Force the broken
        # state explicitly by NULLing the pointer afterwards.
        ba = make_billing_account(dbsession, credits=0)
        from sqlalchemy import text

        dbsession.execute(
            text(
                "UPDATE billing_account SET plan_assignment_id = NULL WHERE id = :id",
            ),
            {"id": ba.id},
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)
        hits = [
            d
            for d in result.discrepancies
            if d.category == "plan_assignment_null_pointer"
            and d.billing_account_id == ba.id
        ]
        assert len(hits) == 1
        assert hits[0].severity == "critical"

    def test_pointer_to_closed_row_is_critical(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(dbsession, credits=0)
        tpl = _make_metered_template(dbsession, name="recon-closed-pointer")
        assignment = BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        # Force the pathological state: pointer set, but the row was
        # closed without the pointer being cleared.
        assignment.ended_at = _dt.datetime.now(_dt.timezone.utc)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)
        hits = [
            d
            for d in result.discrepancies
            if d.category == "plan_assignment_pointer_to_closed_row"
            and d.billing_account_id == ba.id
        ]
        assert len(hits) == 1
        assert hits[0].severity == "critical"

    def test_orphan_pointer_is_critical(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(dbsession, credits=0)
        tpl = _make_metered_template(dbsession, name="recon-orphan-pointer")
        assignment = BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        # Hard-delete the assignment row but force-restore the pointer
        # afterwards (FK is SET NULL on delete, so we have to put it
        # back manually to simulate the corrupt state). Re-pointing at
        # a deleted plan id triggers the FK-on-update check, so flip
        # the session into ``session_replication_role = replica`` for
        # the duration of the corruption — that disables FK trigger
        # enforcement and lets us recreate the exact "orphan pointer"
        # state the reconciliation routine is meant to detect.
        plan_id = assignment.id
        dbsession.delete(assignment)
        dbsession.flush()
        from sqlalchemy import text

        dbsession.execute(text("SET session_replication_role = replica"))
        try:
            dbsession.execute(
                text("UPDATE billing_account SET plan_assignment_id=:pid WHERE id=:id"),
                {"pid": plan_id, "id": ba.id},
            )
        finally:
            dbsession.execute(text("SET session_replication_role = DEFAULT"))
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)
        hits = [
            d
            for d in result.discrepancies
            if d.category == "plan_assignment_orphan_pointer"
            and d.billing_account_id == ba.id
        ]
        assert len(hits) == 1
        assert hits[0].severity == "critical"

    def test_orphan_active_assignment_row_is_critical(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """An active assignment row that no BA points at is corruption."""
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(dbsession, credits=0)
        tpl = _make_metered_template(dbsession, name="recon-orphan-active")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        # Clear the pointer so the active row becomes orphan (the row
        # itself is still ended_at IS NULL).
        from sqlalchemy import text

        dbsession.execute(
            text("UPDATE billing_account SET plan_assignment_id=NULL WHERE id=:id"),
            {"id": ba.id},
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)
        hits = [
            d
            for d in result.discrepancies
            if d.category == "plan_assignment_orphan_active_row"
            and d.billing_account_id == ba.id
        ]
        assert len(hits) == 1
        assert hits[0].severity == "critical"

    def test_healthy_pointer_produces_no_discrepancy(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(dbsession, credits=0)
        tpl = _make_metered_template(dbsession, name="recon-healthy")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)
        plan_categories = {
            "plan_assignment_orphan_pointer",
            "plan_assignment_pointer_to_closed_row",
            "plan_assignment_orphan_active_row",
            "plan_assignment_cross_account_leak",
        }
        hits = [
            d for d in result.discrepancies if d.category in plan_categories
        ]
        assert hits == []


class TestPlanGroupNullPointer:
    """``_check_plan_group_null_pointer`` — plan_group_id IS NULL
    is a schema-invariant violation (column is NOT NULL with a
    server default of 1). Every account must point at *some*
    group; clearing must go through the admin assign endpoint
    pointing at the platform default (id=1), never via direct SQL.
    """

    def test_null_plan_group_id_is_critical(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        # ``BillingAccountDAO.create`` sets plan_group_id to
        # DEFAULT_PLAN_GROUP_ID; force the broken state with a
        # raw UPDATE to simulate a manual SQL slip.
        ba = make_billing_account(dbsession, credits=0)
        from sqlalchemy import text

        dbsession.execute(
            text(
                "UPDATE billing_account "
                "SET plan_group_id = NULL WHERE id = :id",
            ),
            {"id": ba.id},
        )
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession)
        hits = [
            d
            for d in result.discrepancies
            if d.category == "plan_group_null_pointer"
            and d.billing_account_id == ba.id
        ]
        assert len(hits) == 1
        assert hits[0].severity == "critical"

    def test_default_plan_group_pointer_is_quiet(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Pristine accounts (auto-assigned to DEFAULT_PLAN_GROUP_ID)
        must NOT trigger the null-pointer discrepancy — only true
        NULLs do."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        make_billing_account(dbsession, credits=0)
        result = recon_mod.reconcile(session=dbsession)
        assert not any(
            d.category == "plan_group_null_pointer" for d in result.discrepancies
        )


# ============================================================================
# Managed-billing v2 — missed metered invoicing
# ============================================================================


class TestMeteredInvoicingCompleteness:
    def test_missing_recharge_for_closed_period_is_warning(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """METERED account active across a closed period without a
        MONTHLY_COMMIT Recharge for that period is flagged."""
        import orchestra.routines.billing_reconciliation as recon_mod
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )
        from sqlalchemy import text

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_recon_missed",
        )
        tpl = _make_metered_template(dbsession, name="recon-missed-period")
        a = BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        # Backdate started_at well into the past so multiple closed
        # periods elapsed before today.
        old_start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=120)
        dbsession.execute(
            text("UPDATE billing_plan_assignment SET started_at=:ts WHERE id=:id"),
            {"ts": old_start, "id": a.id},
        )
        dbsession.commit()
        # The conftest binds the test session with ``expire_on_commit=False``,
        # so without an explicit expire the ORM identity-map keeps the
        # pre-UPDATE ``started_at`` and the reconciliation query (which
        # walks ``session.query(BillingPlanAssignment, ...)``) sees stale
        # values — making the missed-period scan a no-op.
        dbsession.expire_all()

        result = recon_mod.reconcile(session=dbsession, lookback_days=180)
        hits = [
            d
            for d in result.discrepancies
            if d.category == "metered_invoicing_missed_period"
            and d.billing_account_id == ba.id
        ]
        # At least one missed period within the lookback window.
        assert len(hits) >= 1
        assert all(h.severity == "warning" for h in hits)

    def test_credits_account_is_not_checked(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """A pristine (CREDITS-default) account is out of scope."""
        import orchestra.routines.billing_reconciliation as recon_mod

        monkeypatch.setattr(recon_mod, "stripe", _make_mock_stripe())
        ba = make_billing_account(dbsession, credits=10)
        dbsession.commit()

        result = recon_mod.reconcile(session=dbsession, lookback_days=180)
        hits = [
            d
            for d in result.discrepancies
            if d.category == "metered_invoicing_missed_period"
            and d.billing_account_id == ba.id
        ]
        assert hits == []


# ============================================================================
# Helpers
# ============================================================================


def _empty_stripe_list():
    """Return an object that behaves like an empty Stripe list."""
    return SimpleNamespace(
        auto_paging_iter=lambda: iter([]),
        data=[],
    )
