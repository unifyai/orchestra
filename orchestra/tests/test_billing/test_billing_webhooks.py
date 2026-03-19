"""
Billing webhook handler tests.

Tests call the webhook handler functions directly (e.g.
``process_checkout_session_event``, ``process_invoice_event``,
``handle_event_core``) — **no live Stripe API**.

Sections:
- CheckoutSessionEvent: checkout.session.completed for user & org
- InvoiceEvent: invoice.payment_succeeded / failed idempotency
- ChargeDispute: charge.dispute.created idempotency
- WebhookIdempotency: duplicate event de-duplication
- CheckoutEligibility: spending threshold tracking via checkout
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus, WebhookLog
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    make_org_with_billing,
    make_user_with_billing,
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
# Checkout Session Events
# ============================================================================


class TestCheckoutSessionEvent:
    """Direct tests for process_checkout_session_event."""

    def test_user_checkout_adds_credits(self, dbsession, monkeypatch):
        from orchestra.web.api.webhooks.stripe import process_checkout_session_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_user_ckout",
            credits=0,
            stripe_customer_id="cus_wh_user",
        )
        dbsession.commit()

        event = {
            "id": "evt_user_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": user.id,
                    "amount_total": 5000,
                    "customer": "cus_wh_user",
                    "payment_intent": "pi_wh_user",
                    "metadata": {},
                },
            },
        }

        response = process_checkout_session_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert float(ba.credits) == 50.0

        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type="payment")
            .first()
        )
        assert recharge is not None
        assert recharge.quantity == Decimal("50")
        assert recharge.amount_usd == Decimal("50")
        assert recharge.status == RechargeStatus.PAID

    def test_org_checkout_adds_credits(self, dbsession, monkeypatch):
        from orchestra.web.api.webhooks.stripe import process_checkout_session_event

        org, org_ba = make_org_with_billing(
            dbsession,
            name="Checkout Org",
            stripe_customer_id="cus_wh_org",
            credits=0,
        )
        dbsession.commit()

        event = {
            "id": "evt_org_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": None,
                    "amount_total": 10000,
                    "customer": "cus_wh_org",
                    "payment_intent": "pi_wh_org",
                    "metadata": {"organization_id": str(org.id)},
                },
            },
        }

        response = process_checkout_session_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(org_ba)
        assert float(org_ba.credits) == 100.0

        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=org_ba.id, type="payment")
            .first()
        )
        assert recharge is not None
        assert recharge.quantity == Decimal("100")
        assert recharge.status == RechargeStatus.PAID

    def test_checkout_eligibility_counts_toward_autorecharge(
        self,
        dbsession,
        monkeypatch,
    ):
        """Checkout-created Recharge counts toward auto-recharge eligibility."""
        from orchestra.db.dao.billing_account_dao import (
            MIN_SPEND_FOR_AUTO_RECHARGE,
            BillingAccountDAO,
        )
        from orchestra.web.api.webhooks.stripe import process_checkout_session_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_elig_user",
            credits=0,
            stripe_customer_id="cus_wh_elig",
        )
        dbsession.commit()

        ba_dao = BillingAccountDAO(dbsession)
        assert not ba_dao.can_enable_auto_recharge(ba.id)

        event = {
            "id": "evt_elig_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": user.id,
                    "amount_total": 120000,
                    "customer": "cus_wh_elig",
                    "payment_intent": "pi_wh_elig",
                    "metadata": {},
                },
            },
        }

        process_checkout_session_event(event, dbsession)

        total_spending = ba_dao.get_total_spending(ba.id)
        assert float(total_spending) == 1200.0
        assert total_spending >= MIN_SPEND_FOR_AUTO_RECHARGE
        assert ba_dao.can_enable_auto_recharge(ba.id)

    def test_user_checkout_restores_past_due_to_active(self, dbsession, monkeypatch):
        """PAST_DUE user buying credits is restored to ACTIVE when balance goes positive."""
        from orchestra.web.api.webhooks.stripe import process_checkout_session_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_restore_user",
            credits=-10,
            stripe_customer_id="cus_wh_restore",
            account_status="PAST_DUE",
        )
        dbsession.commit()

        event = {
            "id": "evt_restore_user",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": user.id,
                    "amount_total": 5000,
                    "customer": "cus_wh_restore",
                    "payment_intent": "pi_wh_restore",
                    "metadata": {},
                },
            },
        }

        response = process_checkout_session_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert float(ba.credits) == 40.0
        assert ba.account_status == "ACTIVE"

    def test_user_checkout_stays_past_due_if_still_negative(
        self,
        dbsession,
        monkeypatch,
    ):
        """PAST_DUE user whose checkout doesn't cover the deficit stays PAST_DUE."""
        from orchestra.web.api.webhooks.stripe import process_checkout_session_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_still_pd",
            credits=-100,
            stripe_customer_id="cus_wh_still_pd",
            account_status="PAST_DUE",
        )
        dbsession.commit()

        event = {
            "id": "evt_still_pd",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": user.id,
                    "amount_total": 5000,
                    "customer": "cus_wh_still_pd",
                    "payment_intent": "pi_still_pd",
                    "metadata": {},
                },
            },
        }

        response = process_checkout_session_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert float(ba.credits) == -50.0
        assert ba.account_status == "PAST_DUE"

    def test_org_checkout_restores_past_due_to_active(self, dbsession, monkeypatch):
        """PAST_DUE org buying credits is restored to ACTIVE."""
        from orchestra.web.api.webhooks.stripe import process_checkout_session_event

        org, org_ba = make_org_with_billing(
            dbsession,
            name="Restore Org",
            stripe_customer_id="cus_wh_org_restore",
            credits=-5,
        )
        org_ba.account_status = "PAST_DUE"
        dbsession.commit()

        event = {
            "id": "evt_org_restore",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": None,
                    "amount_total": 10000,
                    "customer": "cus_wh_org_restore",
                    "payment_intent": "pi_org_restore",
                    "metadata": {"organization_id": str(org.id)},
                },
            },
        }

        response = process_checkout_session_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(org_ba)
        assert float(org_ba.credits) == 95.0
        assert org_ba.account_status == "ACTIVE"

    def test_active_account_stays_active_after_checkout(self, dbsession, monkeypatch):
        """Already-ACTIVE account stays ACTIVE (no status change)."""
        from orchestra.web.api.webhooks.stripe import process_checkout_session_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_already_active",
            credits=10,
            stripe_customer_id="cus_wh_active",
            account_status="ACTIVE",
        )
        dbsession.commit()

        event = {
            "id": "evt_stay_active",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": user.id,
                    "amount_total": 2000,
                    "customer": "cus_wh_active",
                    "payment_intent": "pi_stay_active",
                    "metadata": {},
                },
            },
        }

        response = process_checkout_session_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert float(ba.credits) == 30.0
        assert ba.account_status == "ACTIVE"


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
        monkeypatch,
    ):
        """payment_failed for unknown invoice_id resolves via metadata
        and voids credits."""
        import datetime as _dt

        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.lib.time import month_end_utc
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        voided = []
        mock_stripe = SimpleNamespace(
            Invoice=SimpleNamespace(void_invoice=lambda iid: voided.append(iid)),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

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
        assert ba.account_status == "PAST_DUE"
        assert float(ba.credits) == 30  # 80 - 50 voided
        assert voided == ["in_orphan_fail"]


class TestInvoicePaymentFailedVoidsCredits:
    """When an invoice payment definitively fails, the postpaid credits
    that were granted during auto-recharge should be voided."""

    def test_final_failure_voids_credits_and_marks_past_due(
        self,
        dbsession,
        monkeypatch,
    ):
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        voided = []
        mock_stripe = SimpleNamespace(
            Invoice=SimpleNamespace(void_invoice=lambda iid: voided.append(iid)),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "wh_void_user",
            credits=120,
            stripe_customer_id="cus_void",
        )

        r1 = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_void_test",
            type="auto",
        )
        r2 = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("30"),
            amount_usd=Decimal("30.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_void_test",
            type="auto",
        )
        dbsession.add_all([r1, r2])
        dbsession.commit()

        event = {
            "id": "evt_void_final",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_void_test",
                    "status": "uncollectible",
                    "metadata": {},
                },
            },
        }

        response = process_invoice_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(r1)
        dbsession.refresh(r2)
        assert r1.status == RechargeStatus.FAILED
        assert r2.status == RechargeStatus.FAILED

        dbsession.refresh(ba)
        assert ba.account_status == "PAST_DUE"
        assert float(ba.credits) == 40  # 120 - (50 + 30) voided
        assert voided == ["in_void_test"]

    def test_void_stripe_error_is_non_fatal(self, dbsession, monkeypatch):
        """If Stripe void fails, credits are still voided and the webhook
        succeeds — the void is best-effort."""
        import orchestra.web.api.webhooks.stripe as wh_mod
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        def raise_stripe_error(iid):
            raise Exception("Stripe API down")

        mock_stripe = SimpleNamespace(
            Invoice=SimpleNamespace(void_invoice=raise_stripe_error),
            StripeError=Exception,
        )
        monkeypatch.setattr(wh_mod, "stripe", mock_stripe)

        user, ba = make_user_with_billing(
            dbsession,
            "wh_void_err",
            credits=100,
            stripe_customer_id="cus_void_err",
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("60"),
            amount_usd=Decimal("60.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_void_err_test",
            type="auto",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_void_err",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_void_err_test",
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
        assert ba.account_status == "PAST_DUE"
        assert float(ba.credits) == 40  # 100 - 60 voided despite void failure

    def test_intermediate_failure_disables_autorecharge_but_keeps_credits(
        self,
        dbsession,
    ):
        """Non-final failures (Stripe still retrying) leave credits intact
        but disable auto-recharge to prevent compounding debt."""
        from orchestra.web.api.webhooks.stripe import process_invoice_event

        user, ba = make_user_with_billing(
            dbsession,
            "wh_retry_user",
            credits=100,
            stripe_customer_id="cus_retry",
        )
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("10")
        ba.autorecharge_qty = Decimal("50")

        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_retry_test",
            type="auto",
        )
        dbsession.add(rec)
        dbsession.commit()

        event = {
            "id": "evt_retry_1",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_retry_test",
                    "status": "open",
                    "metadata": {},
                },
            },
        }

        response = process_invoice_event(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(rec)
        assert rec.status == RechargeStatus.INVOICE_CREATED  # unchanged

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"  # not degraded
        assert float(ba.credits) == 100  # credits intact
        assert ba.autorecharge is False  # disabled to prevent compounding


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
        ba.autorecharge = True
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("80"),
            amount_usd=Decimal("80"),
            status=RechargeStatus.PAID,
            transaction_id="in_dp_dispute",
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
        assert ba.autorecharge is False
        assert float(ba.credits) == 20  # 100 - 80

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
        ba.autorecharge = True
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
        assert ba.autorecharge is False
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
        ba.autorecharge = False
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
        assert float(ba.credits) == 100  # 40 + 60

        dbsession.refresh(r)
        assert r.status == RechargeStatus.PAID

    def test_won_dispute_does_not_restore_if_other_failed_recharges(
        self,
        dbsession,
        monkeypatch,
    ):
        """When a dispute is won but there are other FAILED recharges,
        the account stays SUSPENDED."""
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
        assert ba.account_status == "SUSPENDED"  # stays suspended
        assert float(ba.credits) == 100  # credits still re-granted

        dbsession.refresh(r_disputed)
        assert r_disputed.status == RechargeStatus.PAID


# ============================================================================
# handle_event_core Dispatch
# ============================================================================


class TestHandleEventCore:
    """Tests for the main event dispatcher."""

    def test_routes_checkout_event(self, dbsession, monkeypatch):
        from orchestra.web.api.webhooks.stripe import handle_event_core

        user, ba = make_user_with_billing(
            dbsession,
            "core_checkout_user",
            credits=0,
            stripe_customer_id="cus_core",
        )
        dbsession.commit()

        event = {
            "id": "evt_core_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": user.id,
                    "amount_total": 2500,
                    "customer": "cus_core",
                    "payment_intent": "pi_core",
                    "metadata": {},
                },
            },
        }

        response = handle_event_core(event, dbsession)
        assert response.status_code == 200

        dbsession.refresh(ba)
        assert float(ba.credits) == 25.0

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
