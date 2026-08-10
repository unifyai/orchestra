"""
Billing webhook handler tests.

Tests call the webhook handler functions directly (e.g.
``process_invoice_event``, ``handle_event_core``) — **no live Stripe API**.

Sections:
- InvoiceEvent: invoice.payment_succeeded / failed idempotency
- ChargeDispute: charge.dispute.created idempotency
- WebhookIdempotency: duplicate event de-duplication
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO
from orchestra.db.models.enums import CommitPeriod
from orchestra.db.models.orchestra_models import (
    DEFAULT_TEMPLATE_ID,
    RECHARGE_TYPE_MONTHLY_COMMIT,
    RECHARGE_TYPE_PROMO,
    BillingPlanTemplate,
    Recharge,
    RechargeStatus,
    WebhookLog,
)
from orchestra.lib.credit_grants import GRANT_KIND_PLAN, grant_expiring_credits
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    TIER_50_ID,
    TIER_75_ID,
    make_user_with_billing,
    put_on_tier,
    subscription_invoice_event,
    template_by_name,
)


@pytest.fixture(autouse=True)
def _env_secrets(monkeypatch):
    import os

    if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    existing_key = os.environ.get("STRIPE_SECRET_KEY")
    if not existing_key or not existing_key.startswith("sk_test_"):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy_for_mocking")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test", raising=False)
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_for_mocking",
        raising=False,
    )
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test", raising=False)


@pytest.fixture(autouse=True)
def _mock_stripe(monkeypatch):
    """Mock Stripe at the webhook module level so handler functions
    don't need a live key."""
    import orchestra.web.api.webhooks.stripe as webhook_module

    dummy = SimpleNamespace(
        PaymentIntent=SimpleNamespace(
            modify=lambda pi_id, **kw: None,
            retrieve=lambda pi_id: {
                "metadata": {"user_id": "test_user", "credits_purchased": "50"},
                "invoice": "in_test_dispute",
            },
        ),
        Customer=SimpleNamespace(
            modify=lambda cid, **kw: None,
        ),
        Webhook=SimpleNamespace(
            construct_event=lambda payload, sig_header, secret, tolerance=None: json.loads(
                payload,
            ),
        ),
        error=SimpleNamespace(
            SignatureVerificationError=Exception,
            StripeError=Exception,
        ),
    )
    monkeypatch.setattr(webhook_module, "stripe", dummy)
    return dummy


def _signed_hdr(body: str) -> str:
    ts = str(int(time.time()))
    sig_raw = f"{ts}.{body}"
    sig = hmac.new(
        settings.STRIPE_WEBHOOK_SECRET.encode(),
        sig_raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={sig}"


# ============================================================================
# Invoice Events
# ============================================================================


class TestInvoiceEvent:
    """Direct tests for process_invoice_event."""

    def test_payment_succeeded_marks_recharges_paid(self, dbsession):
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_inv_user",
            credits=0,
            stripe_customer_id="cus_inv_wh",
        )

        rec = Recharge(
            billing_account_id=ba.id,
            quantity=5,
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_wh_test_1",
            type="usage",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_inv_paid",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_wh_test_1",
                    "status": "paid",
                    "metadata": {"user_id": user.id},
                },
            },
        }

        response = process_invoice_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID

    def test_idempotency(self, dbsession):
        """Same invoice.payment_succeeded event processed only once."""
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_idem_user",
            credits=0,
            stripe_customer_id="cus_idem",
        )

        rec = Recharge(
            billing_account_id=ba.id,
            quantity=5,
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_idem_test",
            type="usage",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_idem_inv",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_idem_test",
                    "status": "paid",
                    "metadata": {"user_id": user.id},
                },
            },
        }

        # Process twice
        for _ in range(2):
            response = process_invoice_event(event, dbsession)
            assert response.status_code == 200

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID
        assert (
            dbsession.query(WebhookLog).filter_by(event_id="evt_idem_inv").count() == 1
        )


# ============================================================================
# Invoice Self-Healing & Credit Voiding
# ============================================================================


class TestInvoiceSelfHealing:
    """When the invoicer's DB commit fails but the Stripe invoice was created,
    the webhook should self-heal by finding orphaned PENDING_INVOICE recharges
    via invoice metadata."""

    def test_self_heal_links_orphaned_recharges_on_success(self, dbsession):
        """payment_succeeded for unknown invoice_id resolves via metadata."""
        import datetime as _dt

        from orchestra.lib.time import month_end_utc
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_heal_user",
            credits=100,
            stripe_customer_id="cus_heal",
        )
        now = _dt.datetime.now(_dt.timezone.utc)
        invoice_group = month_end_utc(now)

        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.PENDING_INVOICE,
            invoice_group=invoice_group,
            type="auto",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_heal_ok",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_orphan_123",
                    "status": "paid",
                    "metadata": {
                        "billing_account_id": str(ba.id),
                        "invoice_group": str(invoice_group),
                    },
                },
            },
        }

        response = process_invoice_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID
        assert rec.stripe_invoice_id == "in_orphan_123"

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

    def test_self_heal_links_orphaned_recharges_on_failure(
        self,
        dbsession,
    ):
        """payment_failed for unknown invoice_id resolves the recharge via
        metadata and marks it FAILED.

        Final failure is a bookkeeping signal only: credits are NOT voided
        and the account is left ACTIVE (the legacy postpaid credit-voiding
        flow was retired with auto-recharge)."""
        import datetime as _dt

        from orchestra.lib.time import month_end_utc
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_heal_fail",
            credits=80,
            stripe_customer_id="cus_heal_f",
        )
        now = _dt.datetime.now(_dt.timezone.utc)
        invoice_group = month_end_utc(now)

        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.PENDING_INVOICE,
            invoice_group=invoice_group,
            type="auto",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_heal_fail",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_orphan_fail",
                    "status": "past_due",
                    "metadata": {
                        "billing_account_id": str(ba.id),
                        "invoice_group": str(invoice_group),
                    },
                },
            },
        }

        response = process_invoice_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.FAILED
        assert rec.stripe_invoice_id == "in_orphan_fail"

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert float(ba.credits) == 80  # credits NOT voided on failure


class TestInvoicePaymentFailedMarksFailed:
    """Final (non-subscription) invoice failure marks the recharge rows
    FAILED as a bookkeeping signal, without voiding credits or auto-voiding
    the Stripe invoice (the legacy auto-recharge debt-settlement flow was
    retired). The invoice represents real, already-incurred usage and is
    left outstanding in Stripe for collection/retry."""

    def test_final_failure_marks_failed_without_voiding(self, dbsession):
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_fail_marks",
            credits=120,
            stripe_customer_id="cus_fail_marks",
        )

        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_fail_marks",
            type="monthly_commit",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_fail_marks",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_fail_marks",
                    "status": "uncollectible",
                    "metadata": {},
                },
            },
        }

        response = process_invoice_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.FAILED

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert float(ba.credits) == 120  # credits untouched


# ============================================================================
# Webhook Idempotency (via HTTP endpoint)
# ============================================================================


class TestWebhookIdempotency:
    """Test idempotency via the full HTTP endpoint."""

    @pytest.mark.anyio
    async def test_invoice_event_idempotent(self, client: AsyncClient, dbsession):
        user, ba = make_user_with_billing(
            dbsession,
            "wh_http_user",
            stripe_customer_id="cus_http_x",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=5,
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_http_test_1",
            type="usage",
        )
        dbsession.add(rec)
        dbsession.commit()

        payload = {
            "id": "evt_http_test",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_http_test_1",
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
        assert (
            dbsession.query(WebhookLog).filter_by(event_id="evt_http_test").count() == 1
        )

    @pytest.mark.anyio
    async def test_charge_dispute_idempotent(self, client: AsyncClient, dbsession):
        user, ba = make_user_with_billing(
            dbsession,
            "wh_dispute_user",
            credits=100,
            stripe_customer_id="cus_dispute_wh",
        )
        dbsession.commit()

        payload = {
            "id": "evt_dispute_wh_test",
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "ch_dispute_wh_123",
                    "payment_intent": "pi_dispute_wh",
                    "invoice": "in_dispute_wh",
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

        logs = (
            dbsession.query(WebhookLog).filter_by(event_id="evt_dispute_wh_test").all()
        )
        assert len(logs) == 1
        assert logs[0].event_type == "charge.dispute.created"


# ============================================================================
# Dispute Handling
# ============================================================================


class TestDisputeCreated:
    """Tests for charge.dispute.created webhook handling."""

    def test_direct_purchase_dispute_suspends_and_deducts(
        self,
        dbsession,
        monkeypatch,
    ):
        """Dispute on a direct credit purchase deducts credits, suspends
        the account, and disables auto-recharge."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {
                        "user_id": "dp_dispute_user",
                        "credits_purchased": "80",
                    },
                    "invoice": "in_dp_dispute",
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "dp_dispute_user",
            credits=100,
            stripe_customer_id="cus_dp_dispute",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("80"),
            amount_usd=Decimal("80"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_dp_dispute",
            type="payment",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_dp_dispute",
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "ch_dp_dispute",
                    "payment_intent": "pi_dp_dispute",
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == "dispute"
        assert float(ba.credits) == 20  # 100 - 80

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.DISPUTED

    def test_invoice_dispute_suspends_and_deducts(
        self,
        dbsession,
        monkeypatch,
    ):
        """Dispute on a monthly invoice deducts credits, marks recharges
        DISPUTED, suspends the account, and disables auto-recharge."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {},
                    "invoice": "in_inv_dispute",
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "inv_dispute_user",
            credits=200,
            stripe_customer_id="cus_inv_dispute",
        )
        r1 = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_inv_dispute",
            type="auto",
        )
        r2 = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("30"),
            amount_usd=Decimal("30"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_inv_dispute",
            type="auto",
        )
        dbsession.add_all([r1, r2])
        dbsession.commit()

        event = {
            "id": "evt_inv_dispute",
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "ch_inv_dispute",
                    "payment_intent": "pi_inv_dispute",
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == "dispute"
        assert float(ba.credits) == 120  # 200 - (50 + 30)

        dbsession.refresh(r1)
        dbsession.refresh(r2)
        assert r1.status == RechargeStatus.DISPUTED
        assert r2.status == RechargeStatus.DISPUTED

    def test_missing_payment_intent_logs_and_succeeds(
        self,
        dbsession,
        monkeypatch,
    ):
        """Dispute event with no payment_intent returns 200 without crashing."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(retrieve=lambda pi_id: {}),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        event = {
            "id": "evt_no_pi_dispute",
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "ch_no_pi",
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200


class TestDisputeClosed:
    """Tests for charge.dispute.closed webhook handling."""

    def test_won_dispute_restores_credits_and_status(
        self,
        dbsession,
        monkeypatch,
    ):
        """When a dispute is won, credits are re-granted and the account
        is restored to ACTIVE (if no other failed recharges exist)."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {
                        "user_id": "won_dispute_user",
                        "credits_purchased": "60",
                    },
                    "invoice": "in_won_dispute",
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "won_dispute_user",
            credits=40,
            stripe_customer_id="cus_won_dispute",
        )
        ba.account_status = "SUSPENDED"
        ba.suspension_reason = "dispute"
        r = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("60"),
            amount_usd=Decimal("60"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_won_dispute",
            type="payment",
        )
        dbsession.add(r)
        dbsession.commit()

        event = {
            "id": "evt_won_dispute",
            "type": "charge.dispute.closed",
            "data": {
                "object": {
                    "id": "dp_won",
                    "status": "won",
                    "payment_intent": "pi_won_dispute",
                    "amount": 6000,
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None
        assert float(ba.credits) == 100  # 40 + 60

        dbsession.refresh(r)
        assert r.status == RechargeStatus.PAID

    def test_won_dispute_does_not_restore_if_other_failed_recharges(
        self,
        dbsession,
        monkeypatch,
    ):
        """When a dispute is won and no other DISPUTED recharges exist,
        the account is restored to ACTIVE (FAILED recharges are settled)."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {
                        "user_id": "won_other_user",
                        "credits_purchased": "60",
                    },
                    "invoice": "in_won_other",
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "won_other_user",
            credits=40,
            stripe_customer_id="cus_won_other",
        )
        ba.account_status = "SUSPENDED"
        ba.suspension_reason = "dispute"
        # The disputed recharge
        r_disputed = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("60"),
            amount_usd=Decimal("60"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_won_other",
            type="payment",
        )
        # Another FAILED recharge from a separate issue
        r_failed = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("30"),
            amount_usd=Decimal("30"),
            status=RechargeStatus.FAILED,
            stripe_invoice_id="in_other_failed",
            type="auto",
        )
        dbsession.add_all([r_disputed, r_failed])
        dbsession.commit()

        event = {
            "id": "evt_won_other",
            "type": "charge.dispute.closed",
            "data": {
                "object": {
                    "id": "dp_won_other",
                    "status": "won",
                    "payment_intent": "pi_won_other",
                    "amount": 6000,
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"  # restored — FAILED is settled debt
        assert ba.suspension_reason is None
        assert float(ba.credits) == 100  # credits re-granted

        dbsession.refresh(r_disputed)
        assert r_disputed.status == RechargeStatus.PAID

    def test_dispute_won_stays_suspended_with_other_disputes(
        self,
        dbsession,
        monkeypatch,
    ):
        """When a dispute is won but another DISPUTED recharge exists,
        the account stays SUSPENDED."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {
                        "user_id": "won_multi_user",
                        "credits_purchased": "40",
                    },
                    "invoice": "in_won_multi",
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "won_multi_user",
            credits=20,
            stripe_customer_id="cus_won_multi",
        )
        ba.account_status = "SUSPENDED"
        ba.suspension_reason = "dispute"
        r_disputed_won = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("40"),
            amount_usd=Decimal("40"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_won_multi",
            type="payment",
        )
        r_disputed_other = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("25"),
            amount_usd=Decimal("25"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_other_dispute",
            type="payment",
        )
        dbsession.add_all([r_disputed_won, r_disputed_other])
        dbsession.commit()

        event = {
            "id": "evt_won_multi",
            "type": "charge.dispute.closed",
            "data": {
                "object": {
                    "id": "dp_won_multi",
                    "status": "won",
                    "payment_intent": "pi_won_multi",
                    "amount": 4000,
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"  # stays — other dispute still open
        assert ba.suspension_reason == "dispute"

    def test_dispute_won_does_not_restore_admin_freeze(
        self,
        dbsession,
        monkeypatch,
    ):
        """When a dispute is won but the account was admin-frozen,
        the account stays SUSPENDED with its admin_freeze reason."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {
                        "user_id": "won_admin_user",
                        "credits_purchased": "50",
                    },
                    "invoice": "in_won_admin",
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "won_admin_user",
            credits=10,
            stripe_customer_id="cus_won_admin",
        )
        ba.account_status = "SUSPENDED"
        ba.suspension_reason = "admin_freeze"
        r = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.DISPUTED,
            stripe_invoice_id="in_won_admin",
            type="payment",
        )
        dbsession.add(r)
        dbsession.commit()

        event = {
            "id": "evt_won_admin",
            "type": "charge.dispute.closed",
            "data": {
                "object": {
                    "id": "dp_won_admin",
                    "status": "won",
                    "payment_intent": "pi_won_admin",
                    "amount": 5000,
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == "admin_freeze"
        assert float(ba.credits) == 60  # credits still restored


# ============================================================================
# Refund Events
# ============================================================================


class TestRefundEvent:
    """Tests for charge.refunded webhook handling."""

    def test_full_refund_deducts_credits_and_marks_failed(
        self,
        dbsession,
        monkeypatch,
    ):
        """Full refund deducts all credits and marks recharge FAILED."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {
                        "user_id": "refund_user",
                        "credits_purchased": "50",
                    },
                    "invoice": None,
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "refund_user",
            credits=80,
            stripe_customer_id="cus_refund",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_refund",
            type="payment",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_full_refund",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_refund",
                    "payment_intent": "pi_refund",
                    "amount": 5000,
                    "amount_refunded": 5000,
                    "invoice": "in_refund",
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert float(ba.credits) == 30  # 80 - 50

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.FAILED

    def test_partial_refund_deducts_proportional_credits(
        self,
        dbsession,
        monkeypatch,
    ):
        """Partial refund deducts proportional credits; recharge stays PAID."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: {
                    "metadata": {
                        "user_id": "partial_refund_user",
                        "credits_purchased": "100",
                    },
                    "invoice": None,
                },
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "partial_refund_user",
            credits=150,
            stripe_customer_id="cus_partial_refund",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("100"),
            amount_usd=Decimal("100"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_partial_refund",
            type="payment",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_partial_refund",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_partial_refund",
                    "payment_intent": "pi_partial_refund",
                    "amount": 10000,
                    "amount_refunded": 5000,
                    "invoice": "in_partial_refund",
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert float(ba.credits) == 100  # 150 - (100 * 0.5)

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID

    def test_stripe_error_on_refund_reraises(
        self,
        dbsession,
        monkeypatch,
    ):
        """Stripe API error during refund processing re-raises so Stripe
        can retry delivery (the rollback removes the WebhookLog in
        production; the test fixture's nested transaction prevents
        verifying that here)."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        class MockStripeError(Exception):
            pass

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: (_ for _ in ()).throw(
                    MockStripeError("API unavailable"),
                ),
            ),
            StripeError=MockStripeError,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        event = {
            "id": "evt_refund_stripe_err",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_stripe_err",
                    "payment_intent": "pi_stripe_err",
                    "amount": 5000,
                    "amount_refunded": 5000,
                },
            },
        }

        with pytest.raises(MockStripeError):
            process_charge_event(event, dbsession)

    def test_missing_payment_intent_on_refund_succeeds(
        self,
        dbsession,
        monkeypatch,
    ):
        """Refund event with no payment_intent returns 200 without crashing."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(retrieve=lambda pi_id: {}),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        event = {
            "id": "evt_refund_no_pi",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_no_pi_refund",
                },
            },
        }

        response = process_charge_event(event, dbsession)
        assert response.status_code == 200


class TestChargeIdempotency:
    """Tests that the charge handler idempotency guard works correctly."""

    def test_duplicate_charge_event_is_skipped(
        self,
        dbsession,
        monkeypatch,
    ):
        """Second delivery of the same charge event is a no-op."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        call_count = {"n": 0}

        def counting_retrieve(pi_id):
            call_count["n"] += 1
            return {
                "metadata": {
                    "user_id": "idem_user",
                    "credits_purchased": "50",
                },
                "invoice": None,
            }

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(retrieve=counting_retrieve),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "idem_user",
            credits=100,
            stripe_customer_id="cus_idem",
        )
        dbsession.commit()

        event = {
            "id": "evt_idem_charge",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_idem",
                    "payment_intent": "pi_idem",
                    "amount": 5000,
                    "amount_refunded": 5000,
                },
            },
        }

        r1 = process_charge_event(event, dbsession)
        assert r1.status_code == 200
        assert call_count["n"] == 1

        dbsession.refresh(ba)
        assert float(ba.credits) == 50  # 100 - 50

        r2 = process_charge_event(event, dbsession)
        assert r2.status_code == 200
        assert call_count["n"] == 1  # Stripe NOT called again

        dbsession.refresh(ba)
        assert float(ba.credits) == 50  # unchanged

    def test_dispute_stripe_error_reraises(
        self,
        dbsession,
        monkeypatch,
    ):
        """Stripe API error during dispute processing re-raises so Stripe
        can retry."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_charge_event

        class MockStripeError(Exception):
            pass

        mock_stripe = SimpleNamespace(
            PaymentIntent=SimpleNamespace(
                retrieve=lambda pi_id: (_ for _ in ()).throw(
                    MockStripeError("timeout"),
                ),
            ),
            StripeError=MockStripeError,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        event = {
            "id": "evt_dispute_stripe_err",
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "dp_stripe_err",
                    "payment_intent": "pi_dispute_err",
                },
            },
        }

        with pytest.raises(MockStripeError):
            process_charge_event(event, dbsession)


# ============================================================================
# customer_cash_balance_transaction.* (wire-transfer breadcrumbs)
# ============================================================================


class TestCashBalanceTransactionEvent:
    """Coverage for ``customer_cash_balance_transaction.*`` handling.

    These are *informational* events emitted when a wire payment via
    ``customer_balance`` flows through Stripe's virtual bank account.
    The recharge → PAID transition still happens through
    ``invoice.payment_succeeded`` once Stripe auto-applies the
    received balance, so this handler must:

    * always 200 (never block Stripe with a 4xx/5xx);
    * never mutate Recharge status (that's the invoice-event handler's
      job, and double-marking would race with it);
    * route through ``handle_event_core`` for *every* sub-type
      (``funded``, ``applied_to_payment``, ``unapplied_from_payment``,
      ``adjusted_for_overdraft``).
    """

    def _event(self, *, txn_type: str, customer: str = "cus_wire_test"):
        return {
            "id": f"evt_cash_balance_{txn_type}",
            # Stripe's actual event name uses underscores throughout
            # (NOT the dotted "customer.*" form used by tax_id /
            # updated). See docs.stripe.com/api/events/types.
            "type": "customer_cash_balance_transaction.created",
            "data": {
                "object": {
                    "customer": customer,
                    "type": txn_type,
                    "currency": "usd",
                    "net_amount": 125000,
                    "ending_balance": 125000,
                },
            },
        }

    def test_funded_event_acks_without_touching_recharges(
        self,
        dbsession,
        caplog,
    ):
        """``funded`` (wire arrived) → 200, no recharge mutation, info log.

        The recharge is still ``INVOICE_CREATED`` after this event;
        only ``invoice.payment_succeeded`` (which Stripe sends after
        auto-applying the balance) is allowed to mark it PAID.
        """
        import logging

        from orchestra.web.api.webhooks.stripe import handle_event_core

        user, ba = make_user_with_billing(
            dbsession,
            "wire_funded_user",
            stripe_customer_id="cus_wire_test",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=10,
            amount_usd=Decimal("1250"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_wire_open",
            type="usage",
        )
        dbsession.add(rec)
        dbsession.commit()

        with caplog.at_level(logging.INFO, logger="orchestra.web.api.webhooks.stripe"):
            response = handle_event_core(
                self._event(txn_type="funded"),
                dbsession,
            )
        assert response.status_code == 200

        dbsession.refresh(rec)
        # Critical: the recharge stays INVOICE_CREATED. Marking it PAID
        # here would race with the invoice.payment_succeeded handler
        # that fires moments later when Stripe auto-applies the
        # balance.
        assert rec.status == RechargeStatus.INVOICE_CREATED

        # And the BillingAccount was correlated by stripe_customer_id
        # so the operator log line is useful.
        assert any(
            "cash_balance_transaction" in (r.getMessage() or "")
            or "cash_balance_transaction" in str(getattr(r, "msg", ""))
            for r in caplog.records
        )

    def test_unapplied_from_payment_logs_warning(self, dbsession, caplog):
        """Overpayment / surplus → WARNING log so ops can decide.

        ``unapplied_from_payment`` (and ``adjusted_for_overdraft``)
        leave money sitting on the customer's cash balance with no
        matching invoice — Stripe won't auto-clear these. We log at
        WARNING so the on-call channel surfaces it.
        """
        import logging

        from orchestra.web.api.webhooks.stripe import handle_event_core

        make_user_with_billing(
            dbsession,
            "wire_overpaid_user",
            stripe_customer_id="cus_wire_test",
        )
        dbsession.commit()

        with caplog.at_level(
            logging.WARNING,
            logger="orchestra.web.api.webhooks.stripe",
        ):
            response = handle_event_core(
                self._event(txn_type="unapplied_from_payment"),
                dbsession,
            )
        assert response.status_code == 200
        # At least one WARNING-level record from the cash-balance
        # handler. (Other modules may emit unrelated warnings; we only
        # care that ours fired.)
        assert any(
            r.levelname == "WARNING"
            and "cash_balance_transaction"
            in (str(r.getMessage()) + str(getattr(r, "msg", "")))
            for r in caplog.records
        )

    def test_unknown_customer_acks_without_error(self, dbsession):
        """Stripe customer not in our DB → still 200.

        Could happen if a Stripe event arrives for a customer that
        was deleted on our side, or for a TEST event replayed against
        prod. Returning anything but 200 makes Stripe retry up to
        3 days; for an info-only event that's just noise.
        """
        from orchestra.web.api.webhooks.stripe import handle_event_core

        response = handle_event_core(
            self._event(txn_type="funded", customer="cus_unknown_to_us"),
            dbsession,
        )
        assert response.status_code == 200


# ============================================================================
# handle_event_core Dispatch
# ============================================================================


class TestHandleEventCore:
    """Tests for the main event dispatcher."""

    def test_routes_invoice_event(self, dbsession):
        from orchestra.web.api.webhooks.stripe import handle_event_core

        user, ba = make_user_with_billing(
            dbsession,
            "core_inv_user",
            stripe_customer_id="cus_core_inv",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=10,
            amount_usd=Decimal("100"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_core_inv",
            type="usage",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_core_inv",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_core_inv",
                    "status": "paid",
                    "metadata": {},
                },
            },
        }

        response = handle_event_core(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.PAID

    def test_unhandled_event_type(self, dbsession):
        from orchestra.web.api.webhooks.stripe import handle_event_core

        event = {
            "id": "evt_unhandled_123",
            "type": "some.unknown.event",
            "data": {"object": {}},
        }

        response = handle_event_core(event, dbsession)
        assert response.status_code == 200

        log = (
            dbsession.query(WebhookLog).filter_by(event_id="evt_unhandled_123").first()
        )
        assert log is not None
        assert log.event_type == "some.unknown.event"


# ============================================================================
# Self-serve subscription webhooks
# ============================================================================


class TestSelfServeSubscriptionWebhooks:
    """Self-serve subscription lifecycle via the webhook handlers.

    Calls ``process_invoice_event`` / ``process_subscription_event`` directly
    with synthetic Stripe event dicts (no live Stripe): ``invoice.paid``
    grant + cycle reset, ``invoice.payment_failed`` soft past-due,
    ``customer.subscription.updated`` tier/status sync, and
    ``customer.subscription.deleted`` revert. Seeded tiers: id 2 = tier_50,
    id 3 = tier_75 (see the ``self_serve_subscription_tiers`` migration).
    """

    def test_invoice_paid_subscription_create_grants_credits(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_create",
            stripe_customer_id="cus_sub_create",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_create")

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_sub_create",
            subscription_id="sub_create",
            billing_reason="subscription_create",
        )
        resp = process_invoice_event(event, dbsession)
        assert resp.status_code == 200

        dao = BillingAccountDAO(dbsession)
        assert dao.get_credits(ba.id) == Decimal("50")

        recharge = (
            dbsession.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.type == RECHARGE_TYPE_MONTHLY_COMMIT,
            )
            .one()
        )
        assert recharge.status == RechargeStatus.PAID
        assert recharge.quantity == Decimal("50")

        # The next-renewal mirror is populated from the invoice period end.
        dbsession.refresh(ba)
        assert ba.current_period_end is not None

    def test_trial_signup_grant_lands_on_first_collected_invoice_only(
        self,
        dbsession: Session,
    ) -> None:
        """The one-time signup grant waits for money, then never re-mints.

        A trial-checkout account (``trial_end_at`` stamped) holds no
        credits until its first invoice actually collects; that invoice
        grants the tier credits plus the one-time signup grant, and the
        renewal cycle grants tier credits only.
        """
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "trial_conv",
            stripe_customer_id="cus_trial_conv",
        )
        ba.trial_end_at = datetime.now(timezone.utc)
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_trial_conv")

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_trial_conv",
            subscription_id="sub_trial_conv",
            billing_reason="subscription_cycle",
        )
        resp = process_invoice_event(event, dbsession)
        assert resp.status_code == 200

        dao = BillingAccountDAO(dbsession)
        signup_grant = Decimal(str(settings.signup_credit_grant))
        assert dao.get_credits(ba.id) == Decimal("50") + signup_grant

        def promo_recharges() -> list[Recharge]:
            return (
                dbsession.query(Recharge)
                .filter(
                    Recharge.billing_account_id == ba.id,
                    Recharge.type == RECHARGE_TYPE_PROMO,
                )
                .all()
            )

        assert len(promo_recharges()) == 1

        renewal = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_trial_conv",
            subscription_id="sub_trial_conv_renewal",
            billing_reason="subscription_cycle",
        )
        resp = process_invoice_event(renewal, dbsession)
        assert resp.status_code == 200

        assert len(promo_recharges()) == 1

    def test_invoice_paid_proration_records_recharge_without_granting(
        self,
        dbsession: Session,
    ) -> None:
        """A ``subscription_update`` (proration) invoice is recorded as a PAID
        Recharge row so the in-app list matches Stripe, but does NOT move the
        wallet (the tier-change endpoint already granted the delta inline)."""
        from orchestra.db.models.orchestra_models import RECHARGE_TYPE_PRORATION
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_prorate",
            stripe_customer_id="cus_sub_prorate",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_prorate")
        credits_before = BillingAccountDAO(dbsession).get_credits(ba.id)

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_sub_prorate",
            subscription_id="sub_prorate",
            billing_reason="subscription_update",
        )
        # $24.00 prorated upgrade charge.
        event["data"]["object"]["amount_paid"] = 2400
        resp = process_invoice_event(event, dbsession)
        assert resp.status_code == 200

        # Wallet untouched — no grant on a proration invoice.
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == credits_before

        # A PAID proration Recharge row was recorded for the invoice list.
        recharge = (
            dbsession.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.type == RECHARGE_TYPE_PRORATION,
            )
            .one()
        )
        assert recharge.status == RechargeStatus.PAID
        assert recharge.amount_usd == Decimal("24")
        assert recharge.stripe_invoice_id == "in_sub_prorate"

        # Idempotent: redelivery of the same invoice doesn't duplicate the row
        # (use a fresh event id so the webhook-log guard doesn't short-circuit).
        event["id"] = "evt_invoice.paid_sub_prorate_again"
        process_invoice_event(event, dbsession)
        rows = (
            dbsession.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.type == RECHARGE_TYPE_PRORATION,
            )
            .all()
        )
        assert len(rows) == 1

    def test_invoice_paid_subscription_create_activates_tier_from_default(
        self,
        dbsession: Session,
    ) -> None:
        """First paid invoice activates the tier — subscribe no longer does.

        The account is still on the free/default tier (activation is deferred
        to first payment so an abandoned checkout never looks subscribed); the
        ``invoice.paid`` (subscription_create) carrying the tier rung activates
        the plan and grants its credits.
        """
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_defer",
            stripe_customer_id="cus_sub_defer",
        )
        ba.stripe_subscription_id = "sub_defer"
        dbsession.flush()

        # Precondition: still on the default tier (not subscribed).
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == DEFAULT_TEMPLATE_ID

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_sub_defer",
            subscription_id="sub_defer",
            billing_reason="subscription_create",
            quantity=50,
        )
        resp = process_invoice_event(event, dbsession)
        assert resp.status_code == 200

        # Tier activated from the invoice rung, and its credits granted.
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_50_ID
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == Decimal("50")

    def test_invoice_paid_basil_schema_routes_and_grants(
        self,
        dbsession: Session,
    ) -> None:
        """A basil-API invoice (subscription id under ``parent``, no top-level
        ``subscription``) still routes to the subscription handler, activates
        the tier and grants credits.

        Regression: the routing guard used ``invoice.subscription`` which the
        2025-05-28 API removed, so first payments silently fell through to the
        legacy no-op path and never granted the cycle's credits.
        """
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_basil",
            stripe_customer_id="cus_sub_basil",
        )
        ba.stripe_subscription_id = "sub_basil"
        dbsession.flush()

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_sub_basil",
            subscription_id="sub_basil",
            billing_reason="subscription_create",
            quantity=50,
            basil=True,
        )
        # Sanity: this fixture carries no top-level subscription id.
        assert event["data"]["object"].get("subscription") is None

        resp = process_invoice_event(event, dbsession)
        assert resp.status_code == 200

        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_50_ID
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == Decimal("50")

    def test_invoice_paid_basil_annual_grants_full_year(
        self,
        dbsession: Session,
    ) -> None:
        """For a basil invoice the annual rung is inferred from the billing
        period span (the line no longer carries a recurring interval), so an
        annual subscription grants the full 12× bucket — not one month."""
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_basil_ann",
            stripe_customer_id="cus_sub_basil_ann",
        )
        ba.stripe_subscription_id = "sub_basil_ann"
        dbsession.flush()

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_sub_basil_ann",
            subscription_id="sub_basil_ann",
            billing_reason="subscription_create",
            quantity=50,
            annual=True,
            basil=True,
        )
        resp = process_invoice_event(event, dbsession)
        assert resp.status_code == 200

        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        template = dbsession.get(BillingPlanTemplate, plan.template_id)
        assert template is not None
        assert template.commit_period == CommitPeriod.ANNUAL
        # 12× the $50 monthly rung granted up front as a single bucket.
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == Decimal("600")

    def test_invoice_paid_subscription_cycle_forfeits_then_regrants(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_cycle",
            stripe_customer_id="cus_sub_cycle",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_cycle")
        dao = BillingAccountDAO(dbsession)

        # Prior cycle's plan grant, partially consumed.
        grant_expiring_credits(
            dbsession,
            ba.id,
            50,
            grant_kind=GRANT_KIND_PLAN,
            expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        dao.deduct_credits(ba.id, 20, category="llm")
        dbsession.flush()
        assert dao.get_credits(ba.id) == Decimal("30")

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_sub_cycle",
            subscription_id="sub_cycle",
            billing_reason="subscription_cycle",
        )
        process_invoice_event(event, dbsession)

        # Prior remainder (30) forfeited, fresh 50 granted → exactly 50.
        assert dao.get_credits(ba.id) == Decimal("50")

    def test_invoice_payment_failed_marks_past_due_soft(
        self,
        dbsession: Session,
    ) -> None:
        """First failure is a *soft* past-due: flagged but still ACTIVE.

        Service must not be cut off during Stripe's dunning/retry window.
        """
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_fail",
            stripe_customer_id="cus_sub_fail",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_fail")

        event = subscription_invoice_event(
            "invoice.payment_failed",
            customer_id="cus_sub_fail",
            subscription_id="sub_fail",
            billing_reason="subscription_cycle",
        )
        process_invoice_event(event, dbsession)

        dbsession.refresh(ba)
        # Soft past-due records ``payment_past_due_at`` and keeps the account
        # cleanly ACTIVE — ``suspension_reason`` stays NULL (reserved for an
        # actual suspension).
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None
        assert ba.payment_past_due_at is not None

    def test_subscription_unpaid_suspends(self, dbsession: Session) -> None:
        """Once Stripe exhausts retries (status=unpaid) the account suspends."""
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_unpaid",
            stripe_customer_id="cus_sub_unpaid",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_unpaid")
        ba.payment_past_due_at = datetime.now(timezone.utc)
        dbsession.flush()

        event = {
            "id": "evt_sub_unpaid",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_unpaid",
                    "customer": "cus_sub_unpaid",
                    "status": "unpaid",
                    "items": {"data": [{"quantity": 50}]},
                },
            },
        }
        process_subscription_event(event, dbsession)

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == "past_due"
        assert ba.payment_past_due_at is not None

    def test_subscription_active_clears_past_due(self, dbsession: Session) -> None:
        """A return to status=active lifts a past-due hold."""
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_recover_status",
            stripe_customer_id="cus_sub_recover_status",
            account_status="SUSPENDED",
        )
        ba.suspension_reason = "past_due"
        ba.payment_past_due_at = datetime.now(timezone.utc)
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_recover_status")

        event = {
            "id": "evt_sub_recover_status",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_recover_status",
                    "customer": "cus_sub_recover_status",
                    "status": "active",
                    "items": {"data": [{"quantity": 50}]},
                },
            },
        }
        process_subscription_event(event, dbsession)

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None
        assert ba.payment_past_due_at is None

    def test_subscription_active_clears_soft_past_due(
        self,
        dbsession: Session,
    ) -> None:
        """A return to status=active clears a *soft* delinquency marker on an
        account that never escalated to suspension."""
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_recover_soft",
            stripe_customer_id="cus_sub_recover_soft",
        )
        ba.payment_past_due_at = datetime.now(timezone.utc)
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_recover_soft")

        event = {
            "id": "evt_sub_recover_soft",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_recover_soft",
                    "customer": "cus_sub_recover_soft",
                    "status": "active",
                    "items": {"data": [{"quantity": 50}]},
                },
            },
        }
        process_subscription_event(event, dbsession)

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None
        assert ba.payment_past_due_at is None

    def test_invoice_paid_clears_past_due(self, dbsession: Session) -> None:
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_recover",
            stripe_customer_id="cus_sub_recover",
            account_status="SUSPENDED",
        )
        ba.suspension_reason = "past_due"
        ba.payment_past_due_at = datetime.now(timezone.utc)
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_recover")

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_sub_recover",
            subscription_id="sub_recover",
            billing_reason="subscription_cycle",
        )
        process_invoice_event(event, dbsession)

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None
        assert ba.payment_past_due_at is None

    def test_subscription_deleted_reverts_to_default(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_del",
            stripe_customer_id="cus_sub_del",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_del")
        grant_expiring_credits(
            dbsession,
            ba.id,
            50,
            grant_kind=GRANT_KIND_PLAN,
            expires_at=datetime.now(timezone.utc) + timedelta(days=10),
        )
        dbsession.flush()

        event = {
            "id": "evt_sub_del",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_del", "customer": "cus_sub_del"}},
        }
        process_subscription_event(event, dbsession)

        dbsession.refresh(ba)
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == DEFAULT_TEMPLATE_ID
        assert ba.stripe_subscription_id is None
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == Decimal("0")

    def test_subscription_incomplete_expired_reverts_to_default(
        self,
        dbsession: Session,
    ) -> None:
        """A new sub whose first payment never completed reverts to free."""
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_incexp",
            stripe_customer_id="cus_sub_incexp",
        )
        # In practice the tier is never activated for an incomplete sub
        # (activation is deferred to invoice.paid); we set it here anyway to
        # prove the revert is robust and clears any lingering tier + sub id.
        ba.stripe_subscription_id = "sub_incexp"
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_incexp")
        dbsession.flush()

        event = {
            "id": "evt_sub_incexp",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_incexp",
                    "customer": "cus_sub_incexp",
                    "status": "incomplete_expired",
                    "items": {"data": [{"quantity": 50}]},
                },
            },
        }
        process_subscription_event(event, dbsession)

        dbsession.refresh(ba)
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == DEFAULT_TEMPLATE_ID
        assert ba.stripe_subscription_id is None

    def test_subscription_updated_syncs_tier(self, dbsession: Session) -> None:
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_upd",
            stripe_customer_id="cus_sub_upd",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_upd")

        event = {
            "id": "evt_sub_upd",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_upd",
                    "customer": "cus_sub_upd",
                    "status": "active",
                    "items": {"data": [{"quantity": 75}]},
                },
            },
        }
        process_subscription_event(event, dbsession)

        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_75_ID

    def test_subscription_updated_syncs_cancel_at_period_end(
        self,
        dbsession: Session,
    ) -> None:
        """``cancel_at_period_end`` on the event mirrors onto the account and
        clears again when the cancellation is undone (parity with Portal)."""
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_cap",
            stripe_customer_id="cus_sub_cap",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_cap")

        def _event(cancel: bool) -> dict:
            return {
                "id": f"evt_sub_cap_{cancel}",
                "type": "customer.subscription.updated",
                "data": {
                    "object": {
                        "id": "sub_cap",
                        "customer": "cus_sub_cap",
                        "status": "active",
                        "cancel_at_period_end": cancel,
                        "items": {"data": [{"quantity": 50}]},
                    },
                },
            }

        process_subscription_event(_event(True), dbsession)
        dbsession.refresh(ba)
        assert ba.subscription_cancel_at_period_end is True

        # Undo (e.g. via the Stripe Portal) clears the flag.
        process_subscription_event(_event(False), dbsession)
        dbsession.refresh(ba)
        assert ba.subscription_cancel_at_period_end is False

    def test_subscription_updated_incomplete_does_not_activate(
        self,
        dbsession: Session,
    ) -> None:
        """An ``updated`` event while the sub is still ``incomplete`` (first
        payment pending) must not activate/sync the tier — otherwise an
        unpaid checkout would look subscribed."""
        from orchestra.web.api.webhooks.stripe import process_subscription_event

        _user, ba = make_user_with_billing(
            dbsession,
            "sub_inc",
            stripe_customer_id="cus_sub_inc",
        )
        ba.stripe_subscription_id = "sub_inc"
        dbsession.flush()
        # Precondition: on the default tier.
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == DEFAULT_TEMPLATE_ID

        event = {
            "id": "evt_sub_inc",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_inc",
                    "customer": "cus_sub_inc",
                    "status": "incomplete",
                    "items": {"data": [{"quantity": 50}]},
                },
            },
        }
        process_subscription_event(event, dbsession)

        # Still on the default tier — no premature activation.
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == DEFAULT_TEMPLATE_ID

    def test_annual_invoice_paid_grants_full_year_bucket(
        self,
        dbsession: Session,
    ) -> None:
        """Annual cycle grants 12× the monthly rung as a single bucket."""
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        _user, ba = make_user_with_billing(
            dbsession,
            "ann_create",
            stripe_customer_id="cus_ann_create",
        )
        tier_50_annual = template_by_name(dbsession, "tier_50_annual")
        put_on_tier(dbsession, ba, tier_50_annual.id, "sub_ann_create")

        event = subscription_invoice_event(
            "invoice.paid",
            customer_id="cus_ann_create",
            subscription_id="sub_ann_create",
            billing_reason="subscription_create",
        )
        process_invoice_event(event, dbsession)

        # 50/mo rung billed annually grants 12 × 50 = 600 credits up front.
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == Decimal("600")

        recharge = (
            dbsession.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.type == RECHARGE_TYPE_MONTHLY_COMMIT,
            )
            .one()
        )
        # Stripe quantity stays the rung (50); credits/grant are 12×.
        assert recharge.quantity == Decimal("50")
        dbsession.refresh(ba)
        assert ba.plan_credits_granted_period == Decimal("600")


# ============================================================================
# Customer profile-flag webhooks — derived, non-PII flags only
# ============================================================================
#
# After moving billing PII to Stripe, the only account-level facts we keep
# locally are two *derived* booleans:
#   * ``is_business`` — maintained by ``customer.tax_id.*`` (a present,
#     non-``unverified`` tax ID flips it on; deletion flips it off).
#   * ``billing_setup_complete`` — the subscribe/tax gate, recomputed from
#     the live address on ``customer.updated`` so it can't go stale when the
#     address is edited outside our PATCH endpoint (e.g. in the Stripe
#     dashboard). The address / tax-ID *values* are never stored locally.
# These handlers had no direct tests; the cases below pin them.


_COMPLETE_ADDRESS = {
    "line1": "1 Test St",
    "city": "San Francisco",
    "postal_code": "94105",
    "country": "US",
}


def _tax_id_event(
    event_type: str,
    *,
    customer_id: str | None,
    value: str | None,
    verification_status: str | None,
    event_id: str,
) -> dict:
    """Synthetic ``customer.tax_id.*`` event dict."""
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "customer": customer_id,
                "value": value,
                "verification": (
                    {"status": verification_status}
                    if verification_status is not None
                    else {}
                ),
            },
        },
    }


def _customer_updated_event(
    *,
    customer_id: str | None,
    address: dict | None,
    event_id: str,
) -> dict:
    """Synthetic ``customer.updated`` event dict."""
    return {
        "id": event_id,
        "type": "customer.updated",
        "data": {"object": {"id": customer_id, "address": address}},
    }


class TestCustomerTaxIdWebhook:
    """``customer.tax_id.*`` maintains only the derived ``is_business`` flag."""

    def test_created_with_verified_tax_id_sets_is_business(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_tax_id_event

        _user, ba = make_user_with_billing(
            dbsession,
            "taxid_verified",
            stripe_customer_id="cus_taxid_verified",
        )
        assert ba.is_business is False

        resp = process_customer_tax_id_event(
            _tax_id_event(
                "customer.tax_id.created",
                customer_id="cus_taxid_verified",
                value="DE123456789",
                verification_status="verified",
                event_id="evt_taxid_verified",
            ),
            dbsession,
        )
        assert resp.status_code == 200
        dbsession.refresh(ba)
        assert ba.is_business is True

    def test_created_pending_tax_id_still_counts_as_business(
        self,
        dbsession: Session,
    ) -> None:
        # Only an explicit ``unverified`` status suppresses the flag; a
        # present ID awaiting verification (``pending``) is treated as a
        # business so the business recurring price applies from the start.
        from orchestra.web.api.webhooks.stripe import process_customer_tax_id_event

        _user, ba = make_user_with_billing(
            dbsession,
            "taxid_pending",
            stripe_customer_id="cus_taxid_pending",
        )

        process_customer_tax_id_event(
            _tax_id_event(
                "customer.tax_id.created",
                customer_id="cus_taxid_pending",
                value="GB999999973",
                verification_status="pending",
                event_id="evt_taxid_pending",
            ),
            dbsession,
        )
        dbsession.refresh(ba)
        assert ba.is_business is True

    def test_created_unverified_tax_id_does_not_set_is_business(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_tax_id_event

        _user, ba = make_user_with_billing(
            dbsession,
            "taxid_unverified",
            stripe_customer_id="cus_taxid_unverified",
        )

        process_customer_tax_id_event(
            _tax_id_event(
                "customer.tax_id.updated",
                customer_id="cus_taxid_unverified",
                value="XX000",
                verification_status="unverified",
                event_id="evt_taxid_unverified",
            ),
            dbsession,
        )
        dbsession.refresh(ba)
        assert ba.is_business is False

    def test_deleted_clears_is_business(self, dbsession: Session) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_tax_id_event

        _user, ba = make_user_with_billing(
            dbsession,
            "taxid_del",
            stripe_customer_id="cus_taxid_del",
        )
        ba.is_business = True
        dbsession.flush()

        process_customer_tax_id_event(
            _tax_id_event(
                "customer.tax_id.deleted",
                customer_id="cus_taxid_del",
                value="DE123456789",
                verification_status="verified",
                event_id="evt_taxid_del",
            ),
            dbsession,
        )
        dbsession.refresh(ba)
        assert ba.is_business is False

    def test_unknown_customer_acks_without_error(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_tax_id_event

        resp = process_customer_tax_id_event(
            _tax_id_event(
                "customer.tax_id.created",
                customer_id="cus_does_not_exist",
                value="DE123456789",
                verification_status="verified",
                event_id="evt_taxid_unknown",
            ),
            dbsession,
        )
        # Acknowledged (no retry storm) even though there's no local account.
        assert resp.status_code == 200

    def test_redelivery_is_idempotent(self, dbsession: Session) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_tax_id_event

        _user, ba = make_user_with_billing(
            dbsession,
            "taxid_idem",
            stripe_customer_id="cus_taxid_idem",
        )
        event = _tax_id_event(
            "customer.tax_id.created",
            customer_id="cus_taxid_idem",
            value="DE123456789",
            verification_status="verified",
            event_id="evt_taxid_idem",
        )
        process_customer_tax_id_event(event, dbsession)
        dbsession.refresh(ba)
        assert ba.is_business is True

        # A manual change followed by a redelivery of the SAME event id must
        # be a no-op (the webhook-log guard short-circuits before reprocessing).
        ba.is_business = False
        dbsession.flush()
        resp = process_customer_tax_id_event(event, dbsession)
        assert resp.status_code == 200
        dbsession.refresh(ba)
        assert ba.is_business is False


class TestCustomerUpdatedWebhook:
    """``customer.updated`` recomputes ``billing_setup_complete`` from the
    live address (self-healing) and never writes PII back."""

    def test_complete_address_sets_setup_complete(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_updated_event

        _user, ba = make_user_with_billing(
            dbsession,
            "cust_addr_ok",
            stripe_customer_id="cus_addr_ok",
        )
        assert ba.billing_setup_complete is False

        resp = process_customer_updated_event(
            _customer_updated_event(
                customer_id="cus_addr_ok",
                address=dict(_COMPLETE_ADDRESS),
                event_id="evt_cust_addr_ok",
            ),
            dbsession,
        )
        assert resp.status_code == 200
        dbsession.refresh(ba)
        assert ba.billing_setup_complete is True

    def test_removed_address_clears_stale_setup_complete(
        self,
        dbsession: Session,
    ) -> None:
        # The staleness case that motivated the self-heal: the holder deletes
        # their address in the Stripe dashboard. The webhook must flip the
        # gate back off so the subscribe flow re-collects a tax-resolvable
        # address instead of trusting a now-stale ``true``.
        from orchestra.web.api.webhooks.stripe import process_customer_updated_event

        _user, ba = make_user_with_billing(
            dbsession,
            "cust_addr_gone",
            stripe_customer_id="cus_addr_gone",
        )
        ba.billing_setup_complete = True
        dbsession.flush()

        resp = process_customer_updated_event(
            _customer_updated_event(
                customer_id="cus_addr_gone",
                address=None,
                event_id="evt_cust_addr_gone",
            ),
            dbsession,
        )
        assert resp.status_code == 200
        dbsession.refresh(ba)
        assert ba.billing_setup_complete is False

    def test_incomplete_address_clears_setup_complete(
        self,
        dbsession: Session,
    ) -> None:
        # A partial address (missing postal_code) is not tax-resolvable, so it
        # counts as incomplete just like a missing one.
        from orchestra.web.api.webhooks.stripe import process_customer_updated_event

        _user, ba = make_user_with_billing(
            dbsession,
            "cust_addr_partial",
            stripe_customer_id="cus_addr_partial",
        )
        ba.billing_setup_complete = True
        dbsession.flush()

        partial = dict(_COMPLETE_ADDRESS)
        partial.pop("postal_code")
        resp = process_customer_updated_event(
            _customer_updated_event(
                customer_id="cus_addr_partial",
                address=partial,
                event_id="evt_cust_addr_partial",
            ),
            dbsession,
        )
        assert resp.status_code == 200
        dbsession.refresh(ba)
        assert ba.billing_setup_complete is False

    def test_unknown_customer_acks_without_error(
        self,
        dbsession: Session,
    ) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_updated_event

        resp = process_customer_updated_event(
            _customer_updated_event(
                customer_id="cus_no_such_account",
                address=dict(_COMPLETE_ADDRESS),
                event_id="evt_cust_unknown",
            ),
            dbsession,
        )
        assert resp.status_code == 200

    def test_redelivery_is_idempotent(self, dbsession: Session) -> None:
        from orchestra.web.api.webhooks.stripe import process_customer_updated_event

        _user, ba = make_user_with_billing(
            dbsession,
            "cust_addr_idem",
            stripe_customer_id="cus_addr_idem",
        )
        event = _customer_updated_event(
            customer_id="cus_addr_idem",
            address=dict(_COMPLETE_ADDRESS),
            event_id="evt_cust_addr_idem",
        )
        process_customer_updated_event(event, dbsession)
        dbsession.refresh(ba)
        assert ba.billing_setup_complete is True

        # Redelivering the same event id after a manual flip is a no-op.
        ba.billing_setup_complete = False
        dbsession.flush()
        resp = process_customer_updated_event(event, dbsession)
        assert resp.status_code == 200
        dbsession.refresh(ba)
        assert ba.billing_setup_complete is False
