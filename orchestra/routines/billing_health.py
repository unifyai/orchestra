"""Billing health snapshot.

Computes aggregate billing metrics from the Orchestra database to
provide an at-a-glance picture of the billing system's health.
Designed to run frequently — all queries are DB-only, no Stripe API calls.

Metrics computed
~~~~~~~~~~~~~~~~

Account landscape
^^^^^^^^^^^^^^^^^
- Status distribution: count of ACTIVE / SUSPENDED accounts.
- At-risk accounts: ACTIVE accounts with credits ≤ 0.
- Total credit balance across all accounts.
- Autorecharge adoption: enabled vs. disabled.

Operational health
^^^^^^^^^^^^^^^^^^
- Recharge activity (last 24 h): count and total USD by status.
- Auto-recharge failure rate: FAILED / total recent auto-recharges.
- Stuck recharges: PENDING_INVOICE or INVOICE_CREATED older than 24 h
  (faster early-warning than the reconciliation's configurable threshold).

Contact provisioning health
^^^^^^^^^^^^^^^^^^^^^^^^^^^
- Contact status distribution: active / grace_period counts.
- Stale billing: active contacts whose ``last_billed_month`` is behind
  the current month (levy routine may have missed them).
- Stuck grace period: contacts in ``grace_period`` for > 14 days
  (suspension routine should have deprovisioned them).
- Active on suspended: contacts still active/grace_period on a billing
  account that is SUSPENDED or CLOSED.
- Cost mismatches: contacts whose recorded ``monthly_cost`` does not
  match the current ``contact_type_costs`` pricing table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    Assistant,
    AssistantContact,
    AssistantContactCost,
    BillingAccount,
    Organization,
    Recharge,
    RechargeStatus,
    User,
)
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AccountSnapshot:
    """Aggregate account status distribution."""

    active: int = 0
    past_due: int = 0
    suspended: int = 0
    total: int = 0
    at_risk: int = 0  # ACTIVE with credits <= 0
    zero_balance: int = 0  # ACTIVE with credits == 0
    negative_balance: int = 0  # ACTIVE with credits < 0
    total_balance: Decimal = Decimal("0")
    autorecharge_enabled: int = 0
    autorecharge_disabled: int = 0

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "past_due": self.past_due,
            "suspended": self.suspended,
            "total": self.total,
            "at_risk": self.at_risk,
            "zero_balance": self.zero_balance,
            "negative_balance": self.negative_balance,
            "total_balance": float(self.total_balance),
            "autorecharge_enabled": self.autorecharge_enabled,
            "autorecharge_disabled": self.autorecharge_disabled,
        }


@dataclass
class RechargeActivity:
    """Recharge activity in a time window."""

    paid_count: int = 0
    paid_usd: Decimal = Decimal("0")
    failed_count: int = 0
    failed_usd: Decimal = Decimal("0")
    pending_count: int = 0
    pending_usd: Decimal = Decimal("0")
    disputed_count: int = 0
    disputed_usd: Decimal = Decimal("0")
    auto_recharge_total: int = 0
    auto_recharge_failed: int = 0
    # Breakdown of PAID recharges by type (payment, auto, promo, etc.)
    paid_by_type: Dict[str, dict] = field(default_factory=dict)

    @property
    def auto_recharge_failure_rate(self) -> float:
        if self.auto_recharge_total == 0:
            return 0.0
        return self.auto_recharge_failed / self.auto_recharge_total

    def to_dict(self) -> dict:
        return {
            "paid": {"count": self.paid_count, "usd": float(self.paid_usd)},
            "failed": {"count": self.failed_count, "usd": float(self.failed_usd)},
            "pending": {"count": self.pending_count, "usd": float(self.pending_usd)},
            "disputed": {"count": self.disputed_count, "usd": float(self.disputed_usd)},
            "paid_by_type": self.paid_by_type,
            "auto_recharge_total": self.auto_recharge_total,
            "auto_recharge_failed": self.auto_recharge_failed,
            "auto_recharge_failure_rate": round(self.auto_recharge_failure_rate, 4),
        }


@dataclass
class ContactSnapshot:
    """Provisioned contact health metrics."""

    active: int = 0
    grace_period: int = 0
    total_monthly_cost: Decimal = Decimal("0")
    stale_billing: int = 0  # active but last_billed_month is behind
    stuck_grace: int = 0  # grace_period > 14 days without suspension
    active_on_suspended: int = 0  # active/grace on SUSPENDED/CLOSED BA
    cost_mismatches: int = 0  # recorded cost != current pricing table

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "grace_period": self.grace_period,
            "total_monthly_cost": float(self.total_monthly_cost),
            "stale_billing": self.stale_billing,
            "stuck_grace": self.stuck_grace,
            "active_on_suspended": self.active_on_suspended,
            "cost_mismatches": self.cost_mismatches,
        }


@dataclass
class InvoiceSnapshot:
    """Aggregate invoice status breakdown.

    Derived from Recharge records that have a ``stripe_invoice_id``.
    """

    paid: int = 0
    paid_usd: Decimal = Decimal("0")
    pending: int = 0
    pending_usd: Decimal = Decimal("0")
    failed: int = 0
    failed_usd: Decimal = Decimal("0")
    uncollectible: int = 0
    uncollectible_usd: Decimal = Decimal("0")

    @property
    def total(self) -> int:
        return self.paid + self.pending + self.failed + self.uncollectible

    def to_dict(self) -> dict:
        return {
            "paid": {"count": self.paid, "usd": float(self.paid_usd)},
            "pending": {"count": self.pending, "usd": float(self.pending_usd)},
            "failed": {"count": self.failed, "usd": float(self.failed_usd)},
            "uncollectible": {
                "count": self.uncollectible,
                "usd": float(self.uncollectible_usd),
            },
            "total": self.total,
        }


@dataclass
class HealthAlert:
    """A health anomaly that warrants attention."""

    category: str
    severity: str  # "critical", "warning", "info"
    detail: str

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass
class HealthReport:
    """Complete billing health report."""

    timestamp: str = ""
    account_snapshot: AccountSnapshot = field(default_factory=AccountSnapshot)
    recharge_activity: RechargeActivity = field(default_factory=RechargeActivity)
    invoice_snapshot: InvoiceSnapshot = field(default_factory=InvoiceSnapshot)
    contact_snapshot: ContactSnapshot = field(default_factory=ContactSnapshot)
    stuck_recharges: int = 0
    alerts: List[HealthAlert] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == "warning")

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "account_snapshot": self.account_snapshot.to_dict(),
            "recharge_activity": self.recharge_activity.to_dict(),
            "invoice_snapshot": self.invoice_snapshot.to_dict(),
            "contact_snapshot": self.contact_snapshot.to_dict(),
            "stuck_recharges": self.stuck_recharges,
            "alerts": [a.to_dict() for a in self.alerts],
            "total_alerts": len(self.alerts),
            "critical": self.critical_count,
            "warnings": self.warning_count,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Thresholds for health alerts
AT_RISK_WARN_THRESHOLD = 5
AUTO_RECHARGE_FAILURE_RATE_WARN = 0.10  # 10 %
STUCK_RECHARGE_WARN_THRESHOLD = 3


def check_health(
    session: Optional[Session] = None,
    *,
    lookback_hours: int = 24,
) -> HealthReport:
    """Run all billing health checks and return a report.

    Args:
        session: DB session.  A new one is created if ``None``.
        lookback_hours: Time window for recharge activity metrics.
    """
    report = HealthReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    own_session = session is None
    if own_session:
        SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
        session = SessionLocal()

    try:
        _compute_account_snapshot(session, report)
        _compute_recharge_activity(session, report, lookback_hours=lookback_hours)
        _compute_stuck_recharges(session, report)
        _compute_invoice_snapshot(session, report)
        _compute_contact_health(session, report)
        _evaluate_alerts(report)
        _log_summary(report)
    except Exception as e:
        report.errors.append(f"Health check failed: {e}")
        logger.exception("Billing health check failed")
    finally:
        if own_session:
            session.close()

    return report


# ---------------------------------------------------------------------------
# Account snapshot
# ---------------------------------------------------------------------------


def _compute_account_snapshot(session: Session, report: HealthReport) -> None:
    """Query aggregate account status distribution."""
    rows = (
        session.query(
            BillingAccount.account_status,
            func.count(BillingAccount.id),
            func.coalesce(func.sum(BillingAccount.credits), 0),
        )
        .group_by(BillingAccount.account_status)
        .all()
    )

    snap = report.account_snapshot
    for status, count, balance in rows:
        snap.total += count
        if status == "ACTIVE":
            snap.active = count
        elif status == "PAST_DUE":
            snap.past_due = count
        elif status == "SUSPENDED":
            snap.suspended = count
        snap.total_balance += balance

    snap.zero_balance = (
        session.query(func.count(BillingAccount.id))
        .filter(
            BillingAccount.account_status == "ACTIVE",
            BillingAccount.credits == 0,
        )
        .scalar()
    ) or 0

    snap.negative_balance = (
        session.query(func.count(BillingAccount.id))
        .filter(
            BillingAccount.account_status == "ACTIVE",
            BillingAccount.credits < 0,
        )
        .scalar()
    ) or 0

    snap.at_risk = snap.zero_balance + snap.negative_balance

    ar_counts = (
        session.query(
            BillingAccount.autorecharge,
            func.count(BillingAccount.id),
        )
        .group_by(BillingAccount.autorecharge)
        .all()
    )
    for enabled, count in ar_counts:
        if enabled:
            snap.autorecharge_enabled = count
        else:
            snap.autorecharge_disabled = count


# ---------------------------------------------------------------------------
# Recharge activity
# ---------------------------------------------------------------------------


def _compute_recharge_activity(
    session: Session,
    report: HealthReport,
    *,
    lookback_hours: int,
) -> None:
    """Aggregate recharge activity in the lookback window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    rows = (
        session.query(
            Recharge.status,
            func.count(Recharge.id),
            func.coalesce(func.sum(Recharge.amount_usd), 0),
        )
        .filter(Recharge.at >= cutoff)
        .group_by(Recharge.status)
        .all()
    )

    activity = report.recharge_activity
    for status, count, total_usd in rows:
        if status == RechargeStatus.PAID or status == RechargeStatus.PAID.value:
            activity.paid_count = count
            activity.paid_usd = total_usd
        elif status == RechargeStatus.FAILED or status == RechargeStatus.FAILED.value:
            activity.failed_count = count
            activity.failed_usd = total_usd
        elif status in (
            RechargeStatus.PENDING_INVOICE,
            RechargeStatus.PENDING_INVOICE.value,
            RechargeStatus.INVOICE_CREATED,
            RechargeStatus.INVOICE_CREATED.value,
        ):
            activity.pending_count += count
            activity.pending_usd += total_usd
        elif (
            status == RechargeStatus.DISPUTED or status == RechargeStatus.DISPUTED.value
        ):
            activity.disputed_count = count
            activity.disputed_usd = total_usd

    # Paid recharges broken down by type (payment, auto, promo, etc.)
    type_rows = (
        session.query(
            Recharge.type,
            func.count(Recharge.id),
            func.coalesce(func.sum(Recharge.amount_usd), 0),
        )
        .filter(
            Recharge.at >= cutoff,
            Recharge.status.in_([RechargeStatus.PAID, RechargeStatus.PAID.value]),
        )
        .group_by(Recharge.type)
        .all()
    )
    for rtype, count, total_usd in type_rows:
        activity.paid_by_type[rtype or "unknown"] = {
            "count": count,
            "usd": float(total_usd),
        }

    auto_rows = (
        session.query(
            Recharge.status,
            func.count(Recharge.id),
        )
        .filter(
            Recharge.at >= cutoff,
            Recharge.type == RECHARGE_TYPE_AUTO,
        )
        .group_by(Recharge.status)
        .all()
    )
    for status, count in auto_rows:
        activity.auto_recharge_total += count
        if status == RechargeStatus.FAILED or status == RechargeStatus.FAILED.value:
            activity.auto_recharge_failed += count


# ---------------------------------------------------------------------------
# Stuck recharges (early warning)
# ---------------------------------------------------------------------------


def _compute_stuck_recharges(session: Session, report: HealthReport) -> None:
    """Count recharges stuck in a pending state for over 24 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    report.stuck_recharges = (
        session.query(func.count(Recharge.id))
        .filter(
            Recharge.status.in_(
                [
                    RechargeStatus.PENDING_INVOICE.value,
                    RechargeStatus.INVOICE_CREATED.value,
                ],
            ),
            Recharge.at < cutoff,
        )
        .scalar()
    ) or 0


# ---------------------------------------------------------------------------
# Invoice snapshot
# ---------------------------------------------------------------------------


def _compute_invoice_snapshot(session: Session, report: HealthReport) -> None:
    """Aggregate all invoice-backed recharges by status.

    Unlike ``RechargeActivity`` (time-windowed, last 24 h), this gives
    a lifetime picture of invoices: how many are paid, still pending
    collection, or failed/uncollectible.

    Only considers recharges that have a ``stripe_invoice_id`` — i.e.
    actual Stripe invoices, not checkout-only purchases.
    """
    snap = report.invoice_snapshot

    rows = (
        session.query(
            Recharge.status,
            func.count(Recharge.id),
            func.coalesce(func.sum(Recharge.amount_usd), 0),
        )
        .filter(Recharge.stripe_invoice_id.isnot(None))
        .group_by(Recharge.status)
        .all()
    )

    for status, count, total_usd in rows:
        status_val = status.value if hasattr(status, "value") else status
        if status_val == RechargeStatus.PAID.value:
            snap.paid = count
            snap.paid_usd = total_usd
        elif status_val in (
            RechargeStatus.PENDING_INVOICE.value,
            RechargeStatus.INVOICE_CREATED.value,
        ):
            snap.pending += count
            snap.pending_usd += total_usd
        elif status_val == RechargeStatus.FAILED.value:
            snap.failed = count
            snap.failed_usd = total_usd
        elif status_val == RechargeStatus.DISPUTED.value:
            snap.uncollectible = count
            snap.uncollectible_usd = total_usd


# ---------------------------------------------------------------------------
# Contact provisioning health
# ---------------------------------------------------------------------------

GRACE_PERIOD_MAX_DAYS = 14


def _compute_contact_health(session: Session, report: HealthReport) -> None:
    """Check provisioned assistant contacts for lifecycle anomalies."""
    snap = report.contact_snapshot
    now = datetime.now(timezone.utc)

    # Current billing month (e.g. "2026-03")
    current_month = now.strftime("%Y-%m")

    # --- Status distribution + total monthly cost -------------------------
    status_rows = (
        session.query(
            AssistantContact.status,
            func.count(AssistantContact.id),
            func.coalesce(func.sum(AssistantContact.monthly_cost), 0),
        )
        .filter(AssistantContact.status.in_(["active", "grace_period"]))
        .group_by(AssistantContact.status)
        .all()
    )
    for status, count, cost in status_rows:
        if status == "active":
            snap.active = count
        elif status == "grace_period":
            snap.grace_period = count
        snap.total_monthly_cost += cost

    # --- Stale billing (active but last_billed_month < current month) -----
    snap.stale_billing = (
        session.query(func.count(AssistantContact.id))
        .join(Assistant, AssistantContact.assistant_id == Assistant.agent_id)
        .filter(
            AssistantContact.status == "active",
            AssistantContact.provisioned_by == "platform",
            Assistant.demo_id.is_(None),
            or_(
                AssistantContact.last_billed_month.is_(None),
                AssistantContact.last_billed_month < current_month,
            ),
        )
        .scalar()
    ) or 0

    # --- Stuck grace period (> 14 days without suspension) ----------------
    grace_cutoff = now - timedelta(days=GRACE_PERIOD_MAX_DAYS)
    snap.stuck_grace = (
        session.query(func.count(AssistantContact.id))
        .filter(
            AssistantContact.status == "grace_period",
            AssistantContact.grace_period_started_at.isnot(None),
            AssistantContact.grace_period_started_at < grace_cutoff,
        )
        .scalar()
    ) or 0

    # --- Active contacts on SUSPENDED/CLOSED billing accounts -------------
    # Chain: AssistantContact -> Assistant -> User -> BillingAccount
    #    or: AssistantContact -> Assistant -> Organization -> BillingAccount
    user_ba_count = (
        session.query(func.count(AssistantContact.id))
        .join(Assistant, AssistantContact.assistant_id == Assistant.agent_id)
        .join(User, Assistant.user_id == User.id)
        .join(BillingAccount, User.billing_account_id == BillingAccount.id)
        .filter(
            AssistantContact.status.in_(["active", "grace_period"]),
            BillingAccount.account_status.in_(["SUSPENDED", "CLOSED"]),
            Assistant.organization_id.is_(None),
        )
        .scalar()
    ) or 0

    org_ba_count = (
        session.query(func.count(AssistantContact.id))
        .join(Assistant, AssistantContact.assistant_id == Assistant.agent_id)
        .join(Organization, Assistant.organization_id == Organization.id)
        .join(BillingAccount, Organization.billing_account_id == BillingAccount.id)
        .filter(
            AssistantContact.status.in_(["active", "grace_period"]),
            BillingAccount.account_status.in_(["SUSPENDED", "CLOSED"]),
        )
        .scalar()
    ) or 0

    snap.active_on_suspended = user_ba_count + org_ba_count

    # --- Cost mismatches (recorded monthly_cost != current pricing) -------
    # Build a lookup of current prices from the cost table
    cost_rows = session.query(
        AssistantContactCost.contact_type,
        AssistantContactCost.provider,
        AssistantContactCost.country_code,
        AssistantContactCost.monthly_cost,
    ).all()
    # Key: (contact_type, provider, country_code) -> monthly_cost
    # Matching logic mirrors AssistantContactDAO.get_contact_monthly_cost:
    #   exact (type, provider, country) -> (type, provider, NULL) -> (type, NULL, NULL)
    price_map: Dict[tuple, Decimal] = {}
    for ct, prov, cc, mc in cost_rows:
        price_map[(ct, prov, cc)] = mc

    if price_map:
        active_contacts = (
            session.query(AssistantContact)
            .filter(
                AssistantContact.status.in_(["active", "grace_period"]),
                AssistantContact.provisioned_by == "platform",
                AssistantContact.monthly_cost.isnot(None),
            )
            .all()
        )
        mismatches = 0
        for c in active_contacts:
            expected = (
                price_map.get((c.contact_type, c.provider, c.country_code))
                or price_map.get((c.contact_type, c.provider, None))
                or price_map.get((c.contact_type, None, None))
            )
            if expected is not None and c.monthly_cost != expected:
                mismatches += 1
        snap.cost_mismatches = mismatches


# ---------------------------------------------------------------------------
# Alert evaluation
# ---------------------------------------------------------------------------


def _evaluate_alerts(report: HealthReport) -> None:
    """Derive actionable alerts from the computed metrics."""
    snap = report.account_snapshot
    activity = report.recharge_activity

    if snap.at_risk >= AT_RISK_WARN_THRESHOLD:
        parts = []
        if snap.zero_balance:
            parts.append(f"{snap.zero_balance} at zero")
        if snap.negative_balance:
            parts.append(f"{snap.negative_balance} negative")
        breakdown = f" ({', '.join(parts)})" if parts else ""
        report.alerts.append(
            HealthAlert(
                category="at_risk_accounts",
                severity="warning",
                detail=(
                    f"{snap.at_risk} ACTIVE accounts have zero or negative "
                    f"credits{breakdown} — billable actions blocked by balance checks"
                ),
            ),
        )

    if snap.past_due > 0:
        report.alerts.append(
            HealthAlert(
                category="past_due_accounts",
                severity="info",
                detail=(
                    f"{snap.past_due} accounts still in legacy PAST_DUE status "
                    f"— should be ACTIVE (balance-based enforcement)"
                ),
            ),
        )

    if (
        activity.auto_recharge_total >= 5
        and activity.auto_recharge_failure_rate >= AUTO_RECHARGE_FAILURE_RATE_WARN
    ):
        pct = activity.auto_recharge_failure_rate * 100
        report.alerts.append(
            HealthAlert(
                category="auto_recharge_failures",
                severity="warning" if pct < 50 else "critical",
                detail=(
                    f"Auto-recharge failure rate is {pct:.1f}% "
                    f"({activity.auto_recharge_failed}/{activity.auto_recharge_total} "
                    f"in the last window)"
                ),
            ),
        )

    if report.stuck_recharges >= STUCK_RECHARGE_WARN_THRESHOLD:
        report.alerts.append(
            HealthAlert(
                category="stuck_recharges",
                severity="warning",
                detail=(
                    f"{report.stuck_recharges} recharges stuck in pending "
                    f"state for over 24 hours"
                ),
            ),
        )

    if activity.disputed_count > 0:
        report.alerts.append(
            HealthAlert(
                category="active_disputes",
                severity="warning",
                detail=(
                    f"{activity.disputed_count} disputed recharges "
                    f"(${float(activity.disputed_usd):.2f}) in the last window"
                ),
            ),
        )

    # --- Contact-specific alerts ---
    contacts = report.contact_snapshot

    if contacts.stale_billing > 0:
        report.alerts.append(
            HealthAlert(
                category="contact_stale_billing",
                severity="warning" if contacts.stale_billing < 10 else "critical",
                detail=(
                    f"{contacts.stale_billing} active contacts not billed for "
                    f"the current month — levy routine may have missed them"
                ),
            ),
        )

    if contacts.stuck_grace > 0:
        report.alerts.append(
            HealthAlert(
                category="contact_stuck_grace",
                severity="warning",
                detail=(
                    f"{contacts.stuck_grace} contacts stuck in grace_period for "
                    f"over {GRACE_PERIOD_MAX_DAYS} days — suspension routine "
                    f"should have deprovisioned them"
                ),
            ),
        )

    if contacts.active_on_suspended > 0:
        report.alerts.append(
            HealthAlert(
                category="contact_active_on_suspended",
                severity="critical",
                detail=(
                    f"{contacts.active_on_suspended} contacts still "
                    f"active/grace_period on SUSPENDED or CLOSED billing "
                    f"accounts — provider costs continue while user isn't paying"
                ),
            ),
        )

    if contacts.cost_mismatches > 0:
        report.alerts.append(
            HealthAlert(
                category="contact_cost_mismatch",
                severity="info",
                detail=(
                    f"{contacts.cost_mismatches} contacts have a recorded "
                    f"monthly_cost that differs from the current pricing table"
                ),
            ),
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_summary(report: HealthReport) -> None:
    """Emit a structured log line for the health check."""
    summary = {
        "message": "Billing health check complete",
        "accounts_total": report.account_snapshot.total,
        "accounts_active": report.account_snapshot.active,
        "accounts_past_due": report.account_snapshot.past_due,
        "accounts_suspended": report.account_snapshot.suspended,
        "at_risk": report.account_snapshot.at_risk,
        "zero_balance": report.account_snapshot.zero_balance,
        "negative_balance": report.account_snapshot.negative_balance,
        "recharges_paid_24h": report.recharge_activity.paid_count,
        "recharges_failed_24h": report.recharge_activity.failed_count,
        "auto_recharge_failure_rate": round(
            report.recharge_activity.auto_recharge_failure_rate,
            4,
        ),
        "stuck_recharges": report.stuck_recharges,
        "invoices_paid": report.invoice_snapshot.paid,
        "invoices_pending": report.invoice_snapshot.pending,
        "invoices_failed": report.invoice_snapshot.failed,
        "invoices_uncollectible": report.invoice_snapshot.uncollectible,
        "contacts_active": report.contact_snapshot.active,
        "contacts_grace_period": report.contact_snapshot.grace_period,
        "contacts_stale_billing": report.contact_snapshot.stale_billing,
        "contacts_stuck_grace": report.contact_snapshot.stuck_grace,
        "contacts_active_on_suspended": report.contact_snapshot.active_on_suspended,
        "contacts_cost_mismatches": report.contact_snapshot.cost_mismatches,
        "alerts": len(report.alerts),
        "errors": len(report.errors),
    }

    if report.critical_count > 0:
        logger.error(summary)
    elif report.warning_count > 0:
        logger.warning(summary)
    else:
        logger.info(summary)
