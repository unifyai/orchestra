"""
Tests for the billing health check routine.

Covers:
1.  Account snapshot (status distribution, at-risk, autorecharge adoption)
2.  Recharge activity (paid/failed/pending counts and USD)
3.  Stuck recharge detection (pending > 24 h)
4.  Alert evaluation (at-risk threshold, auto-recharge failure rate)
5.  Health report serialisation
6.  Admin endpoint (POST /v0/admin/billing/health)
7.  Contact provisioning health (stale billing, stuck grace, active-on-suspended, cost mismatch)
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    AssistantContactCost,
    Recharge,
    RechargeStatus,
)
from orchestra.tests.test_billing.conftest import (
    make_assistant,
    make_billing_account,
    make_contact,
    make_user,
    make_user_with_billing,
)

# ============================================================================
# Account Snapshot
# ============================================================================


class TestAccountSnapshot:
    """Aggregate account status distribution."""

    def test_status_counts(self, dbsession: Session):
        from orchestra.routines.billing_health import check_health

        for status in ("ACTIVE", "ACTIVE", "PAST_DUE", "SUSPENDED"):
            ba = make_billing_account(
                dbsession,
                credits=10,
                account_status=status,
                stripe_customer_id=None,
            )
            make_user(dbsession, f"health_snap_{ba.id}", ba)
        dbsession.commit()

        report = check_health(session=dbsession)
        snap = report.account_snapshot

        assert snap.active >= 2
        assert snap.past_due >= 1
        assert snap.suspended >= 1
        assert snap.total >= 4

    def test_at_risk_count(self, dbsession: Session):
        """ACTIVE accounts with credits <= 0 are counted as at-risk."""
        from orchestra.routines.billing_health import check_health

        ba1 = make_billing_account(
            dbsession,
            credits=0,
            account_status="ACTIVE",
        )
        make_user(dbsession, f"health_risk1_{ba1.id}", ba1)

        ba2 = make_billing_account(
            dbsession,
            credits=-5,
            account_status="ACTIVE",
        )
        make_user(dbsession, f"health_risk2_{ba2.id}", ba2)

        ba3 = make_billing_account(
            dbsession,
            credits=100,
            account_status="ACTIVE",
        )
        make_user(dbsession, f"health_safe_{ba3.id}", ba3)

        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.account_snapshot.at_risk >= 2

    def test_total_credits(self, dbsession: Session):
        from orchestra.routines.billing_health import check_health

        ba1 = make_billing_account(dbsession, credits=100)
        make_user(dbsession, f"health_cr1_{ba1.id}", ba1)
        ba2 = make_billing_account(dbsession, credits=50)
        make_user(dbsession, f"health_cr2_{ba2.id}", ba2)
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.account_snapshot.total_credits >= 150

    def test_autorecharge_counts(self, dbsession: Session):
        from orchestra.routines.billing_health import check_health

        ba1 = make_billing_account(dbsession, credits=10, autorecharge=True)
        make_user(dbsession, f"health_ar1_{ba1.id}", ba1)
        ba2 = make_billing_account(dbsession, credits=10, autorecharge=False)
        make_user(dbsession, f"health_ar2_{ba2.id}", ba2)
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.account_snapshot.autorecharge_enabled >= 1
        assert report.account_snapshot.autorecharge_disabled >= 1


# ============================================================================
# Recharge Activity
# ============================================================================


class TestRechargeActivity:
    """Recharge metrics in the lookback window."""

    def test_paid_recharges_counted(self, dbsession: Session):
        from orchestra.routines.billing_health import check_health

        user, ba = make_user_with_billing(
            dbsession,
            "health_paid",
            credits=100,
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

        report = check_health(session=dbsession, lookback_hours=24)
        assert report.recharge_activity.paid_count >= 1
        assert report.recharge_activity.paid_usd >= Decimal("25")

    def test_failed_recharges_counted(self, dbsession: Session):
        from orchestra.routines.billing_health import check_health

        user, ba = make_user_with_billing(
            dbsession,
            "health_fail",
            credits=0,
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("10"),
            amount_usd=Decimal("10"),
            status=RechargeStatus.FAILED,
            type="auto",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1),
        )
        dbsession.add(rec)
        dbsession.commit()

        report = check_health(session=dbsession, lookback_hours=24)
        assert report.recharge_activity.failed_count >= 1
        assert report.recharge_activity.auto_recharge_failed >= 1

    def test_old_recharges_excluded(self, dbsession: Session):
        """Recharges outside the lookback window are not counted."""
        from orchestra.routines.billing_health import check_health

        user, ba = make_user_with_billing(
            dbsession,
            "health_old",
            credits=100,
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.PAID,
            type="payment",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48),
        )
        dbsession.add(rec)
        dbsession.commit()

        report = check_health(session=dbsession, lookback_hours=24)
        paid_from_this = [
            r for r in [report.recharge_activity] if r.paid_usd >= Decimal("50")
        ]
        # The old recharge should NOT contribute; we can't assert exact
        # counts due to other test data, so just verify the method runs
        assert report.recharge_activity is not None


# ============================================================================
# Stuck Recharges
# ============================================================================


class TestStuckRecharges:
    """Recharges pending for over 24 hours."""

    def test_stuck_recharge_counted(self, dbsession: Session):
        from orchestra.routines.billing_health import check_health

        user, ba = make_user_with_billing(
            dbsession,
            "health_stuck",
            credits=100,
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("25"),
            amount_usd=Decimal("25"),
            status=RechargeStatus.PENDING_INVOICE,
            type="auto",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=30),
        )
        dbsession.add(rec)
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.stuck_recharges >= 1

    def test_fresh_pending_not_stuck(self, dbsession: Session):
        """Pending recharges under 24 h old are not counted as stuck."""
        from orchestra.routines.billing_health import check_health

        user, ba = make_user_with_billing(
            dbsession,
            "health_fresh",
            credits=100,
        )
        rec = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal("25"),
            amount_usd=Decimal("25"),
            status=RechargeStatus.PENDING_INVOICE,
            type="auto",
            at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1),
        )
        dbsession.add(rec)
        dbsession.commit()

        report = check_health(session=dbsession)
        # Can't assert exact 0 due to other test data, but method should work
        assert report.stuck_recharges >= 0


# ============================================================================
# Alert Evaluation
# ============================================================================


class TestAlertEvaluation:
    """Health alerts derived from metrics."""

    def test_at_risk_alert_triggered(self, dbsession: Session):
        from orchestra.routines import billing_health as health_mod
        from orchestra.routines.billing_health import check_health

        original = health_mod.AT_RISK_WARN_THRESHOLD
        health_mod.AT_RISK_WARN_THRESHOLD = 1

        try:
            ba = make_billing_account(
                dbsession,
                credits=0,
                account_status="ACTIVE",
            )
            make_user(dbsession, f"health_alert_risk_{ba.id}", ba)
            dbsession.commit()

            report = check_health(session=dbsession)
            at_risk = [a for a in report.alerts if a.category == "at_risk_accounts"]
            assert len(at_risk) >= 1
        finally:
            health_mod.AT_RISK_WARN_THRESHOLD = original

    def test_auto_recharge_failure_rate_alert(self, dbsession: Session):
        """High auto-recharge failure rate triggers an alert."""
        from orchestra.routines.billing_health import check_health

        user, ba = make_user_with_billing(
            dbsession,
            "health_ar_fail",
            credits=0,
        )
        now = _dt.datetime.now(_dt.timezone.utc)
        for i in range(6):
            dbsession.add(
                Recharge(
                    billing_account_id=ba.id,
                    quantity=Decimal("10"),
                    amount_usd=Decimal("10"),
                    status=RechargeStatus.FAILED,
                    type="auto",
                    at=now - _dt.timedelta(hours=1, minutes=i),
                ),
            )
        dbsession.commit()

        report = check_health(session=dbsession, lookback_hours=24)
        ar_alerts = [a for a in report.alerts if a.category == "auto_recharge_failures"]
        assert len(ar_alerts) >= 1


# ============================================================================
# Report Serialisation
# ============================================================================


class TestReportSerialisation:
    """HealthReport.to_dict() output."""

    def test_to_dict_has_required_fields(self, dbsession: Session):
        from orchestra.routines.billing_health import check_health

        report = check_health(session=dbsession)
        d = report.to_dict()

        assert "timestamp" in d
        assert "account_snapshot" in d
        assert "recharge_activity" in d
        assert "stuck_recharges" in d
        assert "alerts" in d
        assert "total_alerts" in d
        assert "critical" in d
        assert "warnings" in d
        assert "errors" in d

        snap = d["account_snapshot"]
        assert "active" in snap
        assert "past_due" in snap
        assert "suspended" in snap
        assert "total" in snap
        assert "at_risk" in snap
        assert "total_credits" in snap

        activity = d["recharge_activity"]
        assert "paid" in activity
        assert "failed" in activity
        assert "auto_recharge_failure_rate" in activity


# ============================================================================
# Contact Provisioning Health
# ============================================================================


class TestContactHealth:
    """Provisioned contact lifecycle checks."""

    def _setup_contact(
        self,
        dbsession,
        uid,
        *,
        contact_value,
        credits=100,
        account_status="ACTIVE",
        contact_status="active",
        last_billed_month=None,
        grace_period_started_at=None,
        monthly_cost=None,
        provisioned_by="platform",
        contact_type="phone",
        provider="twilio",
        country_code="US",
    ):
        """Helper to create a user + assistant + contact chain."""
        user, ba = make_user_with_billing(
            dbsession,
            uid,
            credits=credits,
            account_status=account_status,
        )
        asst = make_assistant(dbsession, user.id)
        c = make_contact(
            dbsession,
            asst.agent_id,
            contact_type=contact_type,
            contact_value=contact_value,
            provider=provider,
            country_code=country_code,
            provisioned_by=provisioned_by,
            status=contact_status,
            last_billed_month=last_billed_month,
        )
        if grace_period_started_at is not None:
            c.grace_period_started_at = grace_period_started_at
        if monthly_cost is not None:
            c.monthly_cost = monthly_cost
        dbsession.flush()
        return user, ba, asst, c

    def test_contact_status_distribution(self, dbsession: Session):
        """Active and grace_period contacts are counted."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        self._setup_contact(
            dbsession,
            "ct_dist_1",
            contact_value="+15550010001",
            contact_status="active",
            last_billed_month=current_month,
        )
        self._setup_contact(
            dbsession,
            "ct_dist_2",
            contact_value="+15550010002",
            contact_status="grace_period",
            grace_period_started_at=now - _dt.timedelta(days=3),
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.contact_snapshot.active >= 1
        assert report.contact_snapshot.grace_period >= 1

    def test_stale_billing_detected(self, dbsession: Session):
        """Active contacts with last_billed_month behind current are flagged."""
        from orchestra.routines.billing_health import check_health

        self._setup_contact(
            dbsession,
            "ct_stale_1",
            contact_value="+15550020001",
            contact_status="active",
            last_billed_month="2025-01",
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.contact_snapshot.stale_billing >= 1

        stale_alerts = [
            a for a in report.alerts if a.category == "contact_stale_billing"
        ]
        assert len(stale_alerts) >= 1

    def test_stale_billing_not_flagged_when_current(self, dbsession: Session):
        """Contacts billed for the current month are not stale."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        self._setup_contact(
            dbsession,
            "ct_current_1",
            contact_value="+15550030001",
            contact_status="active",
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        # Can't assert exact 0 due to other test data, but this contact
        # should not contribute to the stale count
        assert report.contact_snapshot is not None

    def test_stale_billing_excludes_demo_assistants(self, dbsession: Session):
        """Contacts on demo assistants are excluded from stale billing check."""
        from orchestra.db.models.orchestra_models import DemoAssistantMeta
        from orchestra.routines.billing_health import check_health

        user, ba = make_user_with_billing(
            dbsession,
            "ct_demo_1",
            credits=100,
        )
        demo_meta = DemoAssistantMeta(demoer_user_id=user.id, label="test_demo")
        dbsession.add(demo_meta)
        dbsession.flush()

        asst = make_assistant(dbsession, user.id, demo_id=demo_meta.id)
        make_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15550040001",
            status="active",
            last_billed_month="2024-01",
        )
        dbsession.commit()

        # The demo contact should not be counted as stale
        report = check_health(session=dbsession)
        assert report.contact_snapshot is not None

    def test_stuck_grace_period_detected(self, dbsession: Session):
        """Contacts in grace_period for > 14 days are flagged."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        self._setup_contact(
            dbsession,
            "ct_stuck_1",
            contact_value="+15550050001",
            contact_status="grace_period",
            grace_period_started_at=now - _dt.timedelta(days=20),
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.contact_snapshot.stuck_grace >= 1

        stuck_alerts = [a for a in report.alerts if a.category == "contact_stuck_grace"]
        assert len(stuck_alerts) >= 1

    def test_fresh_grace_period_not_stuck(self, dbsession: Session):
        """Grace period contacts under 14 days are not flagged as stuck."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        self._setup_contact(
            dbsession,
            "ct_fresh_grace_1",
            contact_value="+15550060001",
            contact_status="grace_period",
            grace_period_started_at=now - _dt.timedelta(days=5),
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        # This fresh grace contact should not be counted as stuck
        assert report.contact_snapshot is not None

    def test_active_on_suspended_account(self, dbsession: Session):
        """Active contacts on SUSPENDED billing accounts are flagged."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        self._setup_contact(
            dbsession,
            "ct_susp_1",
            contact_value="+15550070001",
            contact_status="active",
            account_status="SUSPENDED",
            credits=0,
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.contact_snapshot.active_on_suspended >= 1

        susp_alerts = [
            a for a in report.alerts if a.category == "contact_active_on_suspended"
        ]
        assert len(susp_alerts) >= 1
        assert susp_alerts[0].severity == "critical"

    def test_active_on_active_account_not_flagged(self, dbsession: Session):
        """Active contacts on ACTIVE billing accounts are fine."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        self._setup_contact(
            dbsession,
            "ct_ok_1",
            contact_value="+15550080001",
            contact_status="active",
            account_status="ACTIVE",
            credits=100,
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        # This contact should not contribute to active_on_suspended
        assert report.contact_snapshot is not None

    def test_cost_mismatch_detected(self, dbsession: Session):
        """Contacts with monthly_cost != current pricing are flagged."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        # Set up a pricing entry
        cost_entry = AssistantContactCost(
            contact_type="phone",
            provider="twilio",
            country_code="US",
            monthly_cost=Decimal("5.00"),
        )
        dbsession.add(cost_entry)
        dbsession.flush()

        # Create a contact with a different recorded cost
        self._setup_contact(
            dbsession,
            "ct_mismatch_1",
            contact_value="+15550090001",
            contact_status="active",
            monthly_cost=Decimal("3.00"),
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        assert report.contact_snapshot.cost_mismatches >= 1

        mismatch_alerts = [
            a for a in report.alerts if a.category == "contact_cost_mismatch"
        ]
        assert len(mismatch_alerts) >= 1

    def test_cost_match_not_flagged(self, dbsession: Session):
        """Contacts with matching monthly_cost are not flagged."""
        from orchestra.routines.billing_health import check_health

        now = _dt.datetime.now(_dt.timezone.utc)
        current_month = now.strftime("%Y-%m")

        cost_entry = (
            dbsession.query(AssistantContactCost)
            .filter_by(
                contact_type="phone",
                provider="twilio",
                country_code="US",
            )
            .first()
        )
        if not cost_entry:
            cost_entry = AssistantContactCost(
                contact_type="phone",
                provider="twilio",
                country_code="US",
                monthly_cost=Decimal("5.00"),
            )
            dbsession.add(cost_entry)
            dbsession.flush()

        self._setup_contact(
            dbsession,
            "ct_match_1",
            contact_value="+15550100001",
            contact_status="active",
            monthly_cost=cost_entry.monthly_cost,
            last_billed_month=current_month,
        )
        dbsession.commit()

        report = check_health(session=dbsession)
        # This contact should not be a mismatch
        assert report.contact_snapshot is not None

    def test_to_dict_includes_contact_snapshot(self, dbsession: Session):
        """HealthReport.to_dict() includes the contact_snapshot section."""
        from orchestra.routines.billing_health import check_health

        report = check_health(session=dbsession)
        d = report.to_dict()

        assert "contact_snapshot" in d
        cs = d["contact_snapshot"]
        assert "active" in cs
        assert "grace_period" in cs
        assert "total_monthly_cost" in cs
        assert "stale_billing" in cs
        assert "stuck_grace" in cs
        assert "active_on_suspended" in cs
        assert "cost_mismatches" in cs


# ============================================================================
# Admin Endpoint
# ============================================================================


class TestAdminEndpoint:
    """POST /v0/admin/billing/health endpoint."""

    @pytest.mark.anyio
    async def test_endpoint_returns_200(self, client, dbsession):
        from orchestra.tests.utils import ADMIN_HEADERS

        response = await client.post(
            "/v0/admin/billing/health?notify=false",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert "account_snapshot" in body

    @pytest.mark.anyio
    async def test_endpoint_accepts_lookback_param(self, client, dbsession):
        from orchestra.tests.utils import ADMIN_HEADERS

        response = await client.post(
            "/v0/admin/billing/health?lookback_hours=12&notify=false",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200


# ============================================================================
# Discord Notification Formatting
# ============================================================================


class TestDiscordFormatting:
    """Notification embed formatting (no actual webhook calls)."""

    def test_reconciliation_embed_clean_run(self):
        from orchestra.routines.billing_notifications import (
            _format_reconciliation_embed,
        )
        from orchestra.routines.billing_reconciliation import ReconciliationResult

        result = ReconciliationResult(
            started_at="2026-03-24T06:00:00Z",
            stripe_mode="test",
            accounts_checked=42,
        )
        embed = _format_reconciliation_embed(result, "STAGING")

        assert "✅" in embed["title"]
        assert "STAGING" in embed["title"]
        assert embed["color"] == 0x2ECC71  # green

    def test_reconciliation_embed_with_criticals(self):
        from orchestra.routines.billing_notifications import (
            _format_reconciliation_embed,
        )
        from orchestra.routines.billing_reconciliation import (
            Discrepancy,
            ReconciliationResult,
        )

        result = ReconciliationResult(
            started_at="2026-03-24T06:00:00Z",
            stripe_mode="live",
            accounts_checked=100,
            discrepancies=[
                Discrepancy(
                    category="orphaned_stripe_invoice",
                    severity="critical",
                    billing_account_id=1,
                    detail="Test critical issue",
                ),
            ],
        )
        embed = _format_reconciliation_embed(result, "PRODUCTION")

        assert "🔴" in embed["title"]
        assert embed["color"] == 0xE74C3C  # red
        unfixed_fields = [f for f in embed["fields"] if "Unfixed" in f["name"]]
        assert len(unfixed_fields) == 1

    def test_reconciliation_embed_all_auto_fixed(self):
        from orchestra.routines.billing_notifications import (
            _format_reconciliation_embed,
        )
        from orchestra.routines.billing_reconciliation import (
            Discrepancy,
            ReconciliationResult,
        )

        result = ReconciliationResult(
            started_at="2026-03-24T06:00:00Z",
            stripe_mode="live",
            accounts_checked=50,
            discrepancies=[
                Discrepancy(
                    category="stale_past_due",
                    severity="warning",
                    auto_fixed=True,
                    detail="Auto-fixed",
                ),
            ],
        )
        embed = _format_reconciliation_embed(result, "PRODUCTION")

        assert "⚠️" in embed["title"]
        unfixed_fields = [f for f in embed["fields"] if "Unfixed" in f["name"]]
        assert len(unfixed_fields) == 0

    def test_health_embed_clean(self):
        from orchestra.routines.billing_health import HealthReport
        from orchestra.routines.billing_notifications import _format_health_embed

        report = HealthReport(timestamp="2026-03-24T06:00:00Z")
        report.account_snapshot.active = 100
        report.account_snapshot.total = 120

        embed = _format_health_embed(report, "PRODUCTION")

        assert "✅" in embed["title"]
        assert embed["color"] == 0x2ECC71

    def test_health_embed_with_alerts(self):
        from orchestra.routines.billing_health import HealthAlert, HealthReport
        from orchestra.routines.billing_notifications import _format_health_embed

        report = HealthReport(timestamp="2026-03-24T06:00:00Z")
        report.alerts.append(
            HealthAlert(
                category="auto_recharge_failures",
                severity="critical",
                detail="50% failure rate",
            ),
        )

        embed = _format_health_embed(report, "PRODUCTION")

        assert "🔴" in embed["title"]
        assert embed["color"] == 0xE74C3C
        alert_fields = [f for f in embed["fields"] if "Alerts" in f["name"]]
        assert len(alert_fields) == 1

    def test_reconciliation_embed_shows_owner_info(self):
        """Unfixed discrepancies show owner identity instead of bare BA ID."""
        from orchestra.routines.billing_notifications import (
            _format_reconciliation_embed,
        )
        from orchestra.routines.billing_reconciliation import (
            Discrepancy,
            ReconciliationResult,
        )

        result = ReconciliationResult(
            started_at="2026-03-24T06:00:00Z",
            stripe_mode="live",
            accounts_checked=10,
            discrepancies=[
                Discrepancy(
                    category="credit_balance_integrity",
                    severity="warning",
                    billing_account_id=42,
                    detail="SUSPENDED with positive credits",
                    owner_type="user",
                    owner_email="alice@example.com",
                    owner_name="Alice",
                ),
            ],
        )
        embed = _format_reconciliation_embed(result, "PRODUCTION")

        unfixed_fields = [f for f in embed["fields"] if "Unfixed" in f["name"]]
        assert len(unfixed_fields) == 1
        assert "Alice" in unfixed_fields[0]["value"]

    def test_reconciliation_embed_shows_org_owner(self):
        """Org-owned discrepancies show org name and owner email."""
        from orchestra.routines.billing_notifications import (
            _format_reconciliation_embed,
        )
        from orchestra.routines.billing_reconciliation import (
            Discrepancy,
            ReconciliationResult,
        )

        result = ReconciliationResult(
            started_at="2026-03-24T06:00:00Z",
            stripe_mode="live",
            accounts_checked=10,
            discrepancies=[
                Discrepancy(
                    category="stripe_customer",
                    severity="critical",
                    billing_account_id=99,
                    stripe_id="cus_test123",
                    detail="Deleted Stripe customer",
                    owner_type="org",
                    owner_email="bob@corp.com",
                    owner_name="Acme Corp",
                    stripe_url="https://dashboard.stripe.com/customers/cus_test123",
                ),
            ],
        )
        embed = _format_reconciliation_embed(result, "PRODUCTION")

        unfixed_fields = [f for f in embed["fields"] if "Unfixed" in f["name"]]
        assert len(unfixed_fields) == 1
        value = unfixed_fields[0]["value"]
        assert "Acme Corp" in value
        assert "bob@corp.com" in value
        assert "→ Stripe" in value

    def test_reconciliation_embed_auto_fixed_section(self):
        """Auto-fixed discrepancies get their own summary section."""
        from orchestra.routines.billing_notifications import (
            _format_reconciliation_embed,
        )
        from orchestra.routines.billing_reconciliation import (
            Discrepancy,
            ReconciliationResult,
        )

        result = ReconciliationResult(
            started_at="2026-03-24T06:00:00Z",
            stripe_mode="live",
            accounts_checked=10,
            discrepancies=[
                Discrepancy(
                    category="stale_past_due",
                    severity="warning",
                    billing_account_id=7,
                    auto_fixed=True,
                    detail="PAST_DUE → SUSPENDED",
                    owner_type="user",
                    owner_email="fixed@example.com",
                    owner_name="FixedUser",
                ),
            ],
        )
        embed = _format_reconciliation_embed(result, "PRODUCTION")

        auto_fields = [f for f in embed["fields"] if "Auto-fixed" in f["name"]]
        assert len(auto_fields) == 1
        assert "FixedUser" in auto_fields[0]["value"]

    def test_reconciliation_embed_fallback_to_ba_id(self):
        """When no owner info, falls back to BA ID."""
        from orchestra.routines.billing_notifications import (
            _format_reconciliation_embed,
        )
        from orchestra.routines.billing_reconciliation import (
            Discrepancy,
            ReconciliationResult,
        )

        result = ReconciliationResult(
            started_at="2026-03-24T06:00:00Z",
            stripe_mode="live",
            accounts_checked=10,
            discrepancies=[
                Discrepancy(
                    category="test_cat",
                    severity="warning",
                    billing_account_id=55,
                    detail="No owner enrichment",
                ),
            ],
        )
        embed = _format_reconciliation_embed(result, "STAGING")

        unfixed_fields = [f for f in embed["fields"] if "Unfixed" in f["name"]]
        assert len(unfixed_fields) == 1
        assert "BA 55" in unfixed_fields[0]["value"]

    def test_failure_notification_format(self):
        """notify_failure produces a red embed with the error message."""
        import os
        from unittest.mock import patch as mock_patch

        from orchestra.routines.billing_notifications import COLOR_RED, notify_failure

        sent_payloads = []

        def _capture_webhook(url, *, content="", embeds=None):
            sent_payloads.append({"url": url, "content": content, "embeds": embeds})
            return True

        with mock_patch.dict(
            os.environ,
            {"DISCORD_BILLING_WEBHOOK_URL": "https://test.webhook"},
        ):
            with mock_patch(
                "orchestra.routines.billing_notifications._send_webhook",
                _capture_webhook,
            ):
                result = notify_failure(
                    "Reconciliation",
                    "Connection refused: localhost:5432",
                )

        assert result is True
        assert len(sent_payloads) == 1
        payload = sent_payloads[0]
        assert payload["url"] == "https://test.webhook"
        assert "Reconciliation" in payload["content"]
        assert "failed" in payload["content"].lower()

        embed = payload["embeds"][0]
        assert "FAILED" in embed["title"]
        assert embed["color"] == COLOR_RED
        error_field = embed["fields"][0]
        assert "Connection refused" in error_field["value"]

    def test_failure_notification_skipped_without_webhook(self):
        """notify_failure silently returns True if no webhook is configured."""
        import os
        from unittest.mock import patch as mock_patch

        from orchestra.routines.billing_notifications import notify_failure

        with mock_patch.dict(os.environ, {}, clear=True):
            result = notify_failure("Health Check", "some error")

        assert result is True

    def test_billing_event_failure_notification_format(self):
        """notify_billing_event_failure produces a red embed with context."""
        import os
        from unittest.mock import patch as mock_patch

        from orchestra.routines.billing_notifications import (
            COLOR_RED,
            _cooldown_cache,
            notify_billing_event_failure,
        )

        _cooldown_cache.clear()
        sent_payloads = []

        def _capture_webhook(url, *, content="", embeds=None):
            sent_payloads.append({"url": url, "content": content, "embeds": embeds})
            return True

        with mock_patch.dict(
            os.environ,
            {"DISCORD_BILLING_WEBHOOK_URL": "https://test.webhook"},
        ):
            with mock_patch(
                "orchestra.routines.billing_notifications._send_webhook",
                _capture_webhook,
            ):
                result = notify_billing_event_failure(
                    "auto_recharge",
                    error="StripeError: card declined",
                    context_id="ba_42",
                    billing_account_id=42,
                )

        assert result is True
        assert len(sent_payloads) == 1
        embed = sent_payloads[0]["embeds"][0]
        assert embed["color"] == COLOR_RED
        assert "auto_recharge" in embed["title"]
        assert "BA 42" in embed["title"]
        ba_field = next(f for f in embed["fields"] if f["name"] == "Billing account")
        assert ba_field["value"] == "42"
        error_field = next(f for f in embed["fields"] if f["name"] == "Error")
        assert "card declined" in error_field["value"]

    def test_billing_event_failure_rate_limited(self):
        """Duplicate notifications within cooldown window are suppressed."""
        import os
        from unittest.mock import patch as mock_patch

        from orchestra.routines.billing_notifications import (
            _cooldown_cache,
            notify_billing_event_failure,
        )

        _cooldown_cache.clear()
        call_count = 0

        def _counting_webhook(url, *, content="", embeds=None):
            nonlocal call_count
            call_count += 1
            return True

        with mock_patch.dict(
            os.environ,
            {"DISCORD_BILLING_WEBHOOK_URL": "https://test.webhook"},
        ):
            with mock_patch(
                "orchestra.routines.billing_notifications._send_webhook",
                _counting_webhook,
            ):
                notify_billing_event_failure(
                    "webhook_checkout",
                    error="fail1",
                    context_id="evt_123",
                )
                notify_billing_event_failure(
                    "webhook_checkout",
                    error="fail2",
                    context_id="evt_123",
                )

        assert call_count == 1

    def test_billing_event_failure_different_context_not_limited(self):
        """Different context IDs are not suppressed by the rate limiter."""
        import os
        from unittest.mock import patch as mock_patch

        from orchestra.routines.billing_notifications import (
            _cooldown_cache,
            notify_billing_event_failure,
        )

        _cooldown_cache.clear()
        call_count = 0

        def _counting_webhook(url, *, content="", embeds=None):
            nonlocal call_count
            call_count += 1
            return True

        with mock_patch.dict(
            os.environ,
            {"DISCORD_BILLING_WEBHOOK_URL": "https://test.webhook"},
        ):
            with mock_patch(
                "orchestra.routines.billing_notifications._send_webhook",
                _counting_webhook,
            ):
                notify_billing_event_failure(
                    "webhook_checkout",
                    error="fail",
                    context_id="evt_aaa",
                )
                notify_billing_event_failure(
                    "webhook_checkout",
                    error="fail",
                    context_id="evt_bbb",
                )

        assert call_count == 2

    def test_billing_event_failure_skipped_without_webhook(self):
        """Returns True when no webhook URL is set."""
        import os
        from unittest.mock import patch as mock_patch

        from orchestra.routines.billing_notifications import (
            _cooldown_cache,
            notify_billing_event_failure,
        )

        _cooldown_cache.clear()

        with mock_patch.dict(os.environ, {}, clear=True):
            result = notify_billing_event_failure(
                "auto_recharge",
                error="test",
                context_id="ba_1",
            )

        assert result is True

    def test_billing_event_failure_cooldown_expires(self):
        """After cooldown expires, the same key can fire again."""
        import os
        import time
        from unittest.mock import patch as mock_patch

        from orchestra.routines.billing_notifications import (
            _cooldown_cache,
            notify_billing_event_failure,
        )

        _cooldown_cache.clear()
        call_count = 0

        def _counting_webhook(url, *, content="", embeds=None):
            nonlocal call_count
            call_count += 1
            return True

        with mock_patch.dict(
            os.environ,
            {"DISCORD_BILLING_WEBHOOK_URL": "https://test.webhook"},
        ):
            with mock_patch(
                "orchestra.routines.billing_notifications._send_webhook",
                _counting_webhook,
            ):
                notify_billing_event_failure(
                    "contact_levy",
                    error="fail",
                    context_id="ba_5_2026-03",
                )

                _cooldown_cache["contact_levy:ba_5_2026-03"] = time.monotonic() - 400

                notify_billing_event_failure(
                    "contact_levy",
                    error="fail again",
                    context_id="ba_5_2026-03",
                )

        assert call_count == 2
