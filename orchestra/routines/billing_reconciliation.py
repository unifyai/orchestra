"""Stripe ↔ DB billing reconciliation.

Compares the authoritative Stripe state with the Orchestra database
and reports (or auto-fixes) discrepancies.  Designed to run daily via
Cloud Scheduler / GitHub Actions.

Auto-fix tiers
~~~~~~~~~~~~~~

The ``auto_fix`` parameter accepts a tier string that controls which
fixes are applied.  Each tier includes all lower tiers:

``"none"`` (default)
    Detection only — no mutations.

``"safe"``
    Fixes with zero financial impact — purely defensive cleanup:

    - Clear deleted/missing Stripe customer ID + disable autorecharge.
    - Disable autorecharge when no ``stripe_customer_id`` exists.
    - Disable autorecharge when no payment method on file.
    - Dispute lost → status set to ``FAILED`` (credits already voided).
    - Orphaned grace-period contacts → ``active`` (BA has credits ≥ 0).

``"moderate"``
    Includes *safe* plus recharge status corrections:

    - Stale recharge confirmed **paid** by Stripe → ``PAID``.
    - Dispute won → ``PAID`` + credits restored + account ``ACTIVE``.
    - SUSPENDED with no active disputes and reason ``dispute`` or
      ``None`` (legacy) → ``ACTIVE``.  Accounts with reason
      ``admin_freeze`` are always skipped.

``"all"``
    Includes *moderate* plus fixes that mutate credit balances or
    replay external events:

    - Stale recharge void/uncollectible → ``FAILED`` + credits voided.
    - Orphaned paid invoice → create ``Recharge`` + grant credits.
    - Missed webhook → replay event through ``handle_event``.
    - Unvoided FAILED auto-recharge → deduct unearned credits.

Passing ``True`` (bool) is treated as ``"all"`` for backward
compatibility; ``False`` is treated as ``"none"``.

Flag-only checks (never auto-fixed)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- **SUSPENDED + positive credits** — may be intentional (admin hold,
  dispute penalty).  Manual review needed.
- **Duplicate Stripe customers** — DB-level data corruption.
- **Credit balance ceiling** — phantom credit injection detection.
- **SUSPENDED with ``admin_freeze`` reason** — intentional admin action,
  never auto-fixed.

The routine uses the *same* ``STRIPE_SECRET_KEY`` that Orchestra is
configured with — staging instances use a test-mode key, production
uses a live-mode key.  No extra configuration needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import stripe
from sqlalchemy import func
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_AUTO,
    BillingAccount,
    Organization,
    Recharge,
    RechargeStatus,
    User,
    WebhookLog,
)
from orchestra.web.lifetime import get_engine

BILLING_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
        "invoice.payment_action_required",
        "charge.refunded",
        "charge.refund.updated",
        "charge.dispute.created",
        "charge.dispute.closed",
        "charge.dispute.funds_withdrawn",
    },
)

logger = logging.getLogger(__name__)

STALE_THRESHOLD_HOURS = 48

# ---------------------------------------------------------------------------
# Auto-fix tier system
# ---------------------------------------------------------------------------

FIX_NONE = 0
FIX_SAFE = 1
FIX_MODERATE = 2
FIX_ALL = 3

_FIX_TIER_MAP = {
    "none": FIX_NONE,
    "safe": FIX_SAFE,
    "moderate": FIX_MODERATE,
    "all": FIX_ALL,
}


def _parse_fix_level(auto_fix) -> int:  # noqa: ANN001
    """Convert the public ``auto_fix`` parameter to an internal tier int.

    Accepts ``bool`` (backward compat) or a tier string.
    """
    if isinstance(auto_fix, bool):
        return FIX_ALL if auto_fix else FIX_NONE
    if isinstance(auto_fix, int):
        return auto_fix
    return _FIX_TIER_MAP.get(str(auto_fix).lower(), FIX_NONE)


@dataclass
class Discrepancy:
    """A single reconciliation discrepancy."""

    category: str
    severity: str  # "critical", "warning", "info"
    billing_account_id: Optional[int] = None
    stripe_id: Optional[str] = None
    detail: str = ""
    auto_fixed: bool = False
    # Enrichment fields (populated post-collection by _enrich_discrepancies)
    owner_type: Optional[str] = None  # "user", "org", or None
    owner_email: Optional[str] = None
    owner_name: Optional[str] = None
    stripe_url: Optional[str] = None
    recharge_context: Optional[List[Dict]] = None


@dataclass
class ReconciliationResult:
    """Summary of a reconciliation run."""

    started_at: str = ""
    finished_at: str = ""
    stripe_mode: str = ""
    accounts_checked: int = 0
    recharges_checked: int = 0
    invoices_checked: int = 0
    disputes_checked: int = 0
    events_checked: int = 0
    discrepancies: List[Discrepancy] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.severity == "warning")

    @property
    def auto_fixed_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.auto_fixed)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stripe_mode": self.stripe_mode,
            "accounts_checked": self.accounts_checked,
            "recharges_checked": self.recharges_checked,
            "invoices_checked": self.invoices_checked,
            "disputes_checked": self.disputes_checked,
            "events_checked": self.events_checked,
            "total_discrepancies": len(self.discrepancies),
            "critical": self.critical_count,
            "warnings": self.warning_count,
            "auto_fixed": self.auto_fixed_count,
            "errors": self.errors,
            "discrepancies": [
                {
                    "category": d.category,
                    "severity": d.severity,
                    "billing_account_id": d.billing_account_id,
                    "stripe_id": d.stripe_id,
                    "detail": d.detail,
                    "auto_fixed": d.auto_fixed,
                    "owner_type": d.owner_type,
                    "owner_email": d.owner_email,
                    "owner_name": d.owner_name,
                    "stripe_url": d.stripe_url,
                    "recharge_context": d.recharge_context,
                }
                for d in self.discrepancies
            ],
        }


def reconcile(
    session: Optional[Session] = None,
    *,
    auto_fix: "bool | str" = "none",
    lookback_days: int = 30,
    stale_hours: int = STALE_THRESHOLD_HOURS,
) -> ReconciliationResult:
    """Run the full reconciliation suite.

    Args:
        session: DB session.  A new one is created if ``None``.
        auto_fix: Tier of auto-fixes to apply: ``"none"`` (default),
            ``"safe"``, ``"moderate"``, or ``"all"``.  Also accepts
            ``bool`` for backward compatibility (``True`` → ``"all"``).
        lookback_days: How far back to check Stripe invoices.
        stale_hours: Recharges older than this in a pending state are
            considered stale and reconciled against Stripe.

    Returns:
        :class:`ReconciliationResult` with all findings.
    """
    from orchestra.lib.billing import configure_stripe

    configure_stripe()
    fix_level = _parse_fix_level(auto_fix)

    if session is not None:
        return _reconcile_with_session(
            session,
            fix_level=fix_level,
            lookback_days=lookback_days,
            stale_hours=stale_hours,
        )

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        return _reconcile_with_session(
            session,
            fix_level=fix_level,
            lookback_days=lookback_days,
            stale_hours=stale_hours,
        )


def _reconcile_with_session(
    session: Session,
    *,
    fix_level: int,
    lookback_days: int,
    stale_hours: int,
) -> ReconciliationResult:
    result = ReconciliationResult(
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    # Detect Stripe mode from the configured key
    try:
        key = stripe.api_key or ""
        if key.startswith("sk_test_"):
            result.stripe_mode = "test"
        elif key.startswith("sk_live_"):
            result.stripe_mode = "live"
        else:
            result.stripe_mode = "unknown"
    except Exception:
        result.stripe_mode = "unknown"

    _check_stale_recharges(
        session,
        result,
        fix_level=fix_level,
        stale_hours=stale_hours,
    )
    _check_stuck_disputes(session, result, fix_level=fix_level, stale_hours=stale_hours)
    _check_stripe_customers(session, result, fix_level=fix_level)
    _check_orphaned_invoices(
        session,
        result,
        fix_level=fix_level,
        lookback_days=lookback_days,
    )
    _check_credit_balance_integrity(session, result, fix_level=fix_level)
    _check_duplicate_stripe_customers(session, result)
    _check_payment_methods(session, result, fix_level=fix_level)
    _check_credit_balance_ceiling(session, result)
    _check_webhook_gaps(
        session,
        result,
        lookback_days=lookback_days,
        fix_level=fix_level,
    )
    _check_failed_recharge_voids(session, result, fix_level=fix_level)
    _check_orphaned_grace_periods(session, result, fix_level=fix_level)
    _check_unjustified_suspensions(session, result, fix_level=fix_level)

    if fix_level > FIX_NONE and result.auto_fixed_count > 0:
        session.commit()

    try:
        _enrich_discrepancies(session, result)
    except Exception as e:
        result.errors.append(f"Enrichment failed: {e}")
        logger.warning("Discrepancy enrichment failed", exc_info=True)

    result.finished_at = datetime.now(timezone.utc).isoformat()

    _log_summary(result)
    return result


# ---------------------------------------------------------------------------
# Check 1: Stale pending recharges
# ---------------------------------------------------------------------------


def _check_stale_recharges(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int,
    stale_hours: int,
) -> None:
    """Find PENDING_INVOICE / INVOICE_CREATED recharges older than
    ``stale_hours`` and verify their status against Stripe.

    Tier: paid confirmation → moderate, void + credit voiding → all.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)

    stale_recharges: List[Recharge] = (
        session.query(Recharge)
        .filter(
            Recharge.status.in_(
                [
                    RechargeStatus.PENDING_INVOICE,
                    RechargeStatus.INVOICE_CREATED,
                ],
            ),
            Recharge.at < cutoff,
        )
        .all()
    )

    result.recharges_checked += len(stale_recharges)

    for recharge in stale_recharges:
        if not recharge.stripe_invoice_id:
            if recharge.status == RechargeStatus.PENDING_INVOICE:
                # PENDING_INVOICE without a stripe_invoice_id is expected
                # until the monthly invoicer runs.  Only flag if very old.
                age_hours = (
                    datetime.now(timezone.utc)
                    - recharge.at.replace(tzinfo=timezone.utc)
                ).total_seconds() / 3600
                if age_hours > stale_hours * 3:
                    result.discrepancies.append(
                        Discrepancy(
                            category="stale_pending_recharge",
                            severity="warning",
                            billing_account_id=recharge.billing_account_id,
                            detail=(
                                f"Recharge {recharge.id} has been PENDING_INVOICE "
                                f"for {age_hours:.0f}h with no Stripe invoice"
                            ),
                        ),
                    )
            continue

        try:
            inv = stripe.Invoice.retrieve(recharge.stripe_invoice_id)
        except stripe.InvalidRequestError:
            result.discrepancies.append(
                Discrepancy(
                    category="missing_stripe_invoice",
                    severity="critical",
                    billing_account_id=recharge.billing_account_id,
                    stripe_id=recharge.stripe_invoice_id,
                    detail=(
                        f"Recharge {recharge.id} references invoice "
                        f"{recharge.stripe_invoice_id} which does not exist in Stripe"
                    ),
                ),
            )
            continue
        except stripe.StripeError as e:
            result.errors.append(
                f"Stripe API error checking invoice {recharge.stripe_invoice_id}: {e}",
            )
            continue

        stripe_status = inv.get("status", "unknown")

        if recharge.status == RechargeStatus.INVOICE_CREATED:
            if stripe_status == "paid":
                fix = fix_level >= FIX_MODERATE
                result.discrepancies.append(
                    Discrepancy(
                        category="recharge_status_mismatch",
                        severity="critical",
                        billing_account_id=recharge.billing_account_id,
                        stripe_id=recharge.stripe_invoice_id,
                        detail=(
                            f"Recharge {recharge.id} is INVOICE_CREATED in DB "
                            f"but Stripe invoice is 'paid' — missed webhook?"
                        ),
                        auto_fixed=fix,
                    ),
                )
                if fix:
                    recharge.status = RechargeStatus.PAID
                    logger.info(
                        "Auto-fixed recharge %s: INVOICE_CREATED → PAID",
                        recharge.id,
                    )
            elif stripe_status in ("void", "uncollectible"):
                fix = fix_level >= FIX_ALL
                result.discrepancies.append(
                    Discrepancy(
                        category="recharge_status_mismatch",
                        severity="critical",
                        billing_account_id=recharge.billing_account_id,
                        stripe_id=recharge.stripe_invoice_id,
                        detail=(
                            f"Recharge {recharge.id} is INVOICE_CREATED in DB "
                            f"but Stripe invoice is '{stripe_status}' — "
                            f"credits may need voiding"
                        ),
                        auto_fixed=fix,
                    ),
                )
                if fix:
                    recharge.status = RechargeStatus.FAILED
                    ba = (
                        session.query(BillingAccount)
                        .filter_by(id=recharge.billing_account_id)
                        .with_for_update()
                        .first()
                    )
                    if ba:
                        ba.credits -= recharge.quantity
                        logger.info(
                            "Auto-fixed recharge %s: INVOICE_CREATED → FAILED, "
                            "voided %s credits from BA %s",
                            recharge.id,
                            recharge.quantity,
                            ba.id,
                        )


# ---------------------------------------------------------------------------
# Check 2: Stripe customer health
# ---------------------------------------------------------------------------


def _check_stripe_customers(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int = FIX_NONE,
) -> None:
    """Verify that billing accounts with ``stripe_customer_id`` still
    have a live (non-deleted) Stripe customer.

    Tier: safe — clears dangling reference, disables autorecharge.
    """
    auto_fix = fix_level >= FIX_SAFE
    accounts: List[BillingAccount] = (
        session.query(BillingAccount)
        .filter(
            BillingAccount.stripe_customer_id.isnot(None),
            BillingAccount.account_status == "ACTIVE",
        )
        .all()
    )

    result.accounts_checked += len(accounts)

    for ba in accounts:
        try:
            customer = stripe.Customer.retrieve(ba.stripe_customer_id)
            if getattr(customer, "deleted", False):
                old_cid = ba.stripe_customer_id
                result.discrepancies.append(
                    Discrepancy(
                        category="deleted_stripe_customer",
                        severity="critical",
                        billing_account_id=ba.id,
                        stripe_id=old_cid,
                        detail=(
                            f"BA {ba.id} has stripe_customer_id "
                            f"{old_cid} but the customer is "
                            f"deleted in Stripe (status={ba.account_status})"
                        ),
                        auto_fixed=auto_fix,
                    ),
                )
                if auto_fix:
                    ba.stripe_customer_id = None
                    ba.autorecharge = False
                    logger.info(
                        "Auto-fixed BA %s: cleared deleted Stripe customer "
                        "%s, disabled autorecharge",
                        ba.id,
                        old_cid,
                    )
        except stripe.InvalidRequestError:
            old_cid = ba.stripe_customer_id
            result.discrepancies.append(
                Discrepancy(
                    category="missing_stripe_customer",
                    severity="critical",
                    billing_account_id=ba.id,
                    stripe_id=old_cid,
                    detail=(
                        f"BA {ba.id} has stripe_customer_id "
                        f"{old_cid} which does not exist "
                        f"in Stripe"
                    ),
                    auto_fixed=auto_fix,
                ),
            )
            if auto_fix:
                ba.stripe_customer_id = None
                ba.autorecharge = False
                logger.info(
                    "Auto-fixed BA %s: cleared missing Stripe customer "
                    "%s, disabled autorecharge",
                    ba.id,
                    old_cid,
                )
        except stripe.StripeError as e:
            result.errors.append(
                f"Stripe API error checking customer {ba.stripe_customer_id}: {e}",
            )


# ---------------------------------------------------------------------------
# Check 3: Orphaned Stripe invoices
# ---------------------------------------------------------------------------


def _check_orphaned_invoices(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int,
    lookback_days: int,
) -> None:
    """Fetch recent Stripe invoices and verify each has a corresponding
    recharge in the DB.  Invoices created by our system carry metadata
    with ``billing_account_id``; invoices without that metadata are
    likely from Checkout and are matched via their ``payment_intent``.

    Auto-fix: for paid invoices with our metadata (``billing_account_id``)
    that have no Recharge row, creates a PAID Recharge and credits the
    account.  The user already paid — this ensures they get the credits.
    """
    lookback_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp(),
    )

    try:
        invoices = stripe.Invoice.list(
            created={"gte": lookback_ts},
            limit=100,
            status="paid",
        )
    except stripe.StripeError as e:
        result.errors.append(f"Failed to list Stripe invoices: {e}")
        return

    for inv in invoices.auto_paging_iter():
        result.invoices_checked += 1
        invoice_id = inv["id"]

        recharge_exists = (
            session.query(Recharge).filter_by(stripe_invoice_id=invoice_id).first()
            is not None
        )

        if recharge_exists:
            continue

        metadata = inv.get("metadata", {})
        ba_id_str = metadata.get("billing_account_id")

        if ba_id_str:
            amount_cents = inv.get("amount_paid", 0)
            credits_amount = Decimal(str(amount_cents)) / 100

            fixed = False
            if fix_level >= FIX_ALL and credits_amount > 0:
                try:
                    ba_id = int(ba_id_str)
                    ba = (
                        session.query(BillingAccount)
                        .filter_by(id=ba_id)
                        .with_for_update()
                        .first()
                    )
                    if ba:
                        new_recharge = Recharge(
                            billing_account_id=ba_id,
                            type=RECHARGE_TYPE_AUTO,
                            quantity=credits_amount,
                            amount_usd=credits_amount,
                            status=RechargeStatus.PAID.value,
                            stripe_invoice_id=invoice_id,
                        )
                        session.add(new_recharge)
                        ba.credits += credits_amount
                        if (
                            ba.account_status == "SUSPENDED"
                            and ba.suspension_reason != "admin_freeze"
                        ):
                            ba.account_status = "ACTIVE"
                            ba.suspension_reason = None
                        fixed = True
                        logger.info(
                            "Auto-fixed orphaned invoice %s: created PAID "
                            "Recharge and credited %s to BA %s",
                            invoice_id,
                            credits_amount,
                            ba_id,
                        )
                except Exception as e:
                    result.errors.append(
                        f"Failed to auto-fix orphaned invoice {invoice_id}: {e}",
                    )

            result.discrepancies.append(
                Discrepancy(
                    category="orphaned_stripe_invoice",
                    severity="critical",
                    billing_account_id=int(ba_id_str),
                    stripe_id=invoice_id,
                    detail=(
                        f"Stripe invoice {invoice_id} (paid, "
                        f"${float(credits_amount):.2f}) references "
                        f"BA {ba_id_str} but has no matching Recharge row"
                    ),
                    auto_fixed=fixed,
                ),
            )
        else:
            customer_id = inv.get("customer")
            if customer_id:
                ba = (
                    session.query(BillingAccount)
                    .filter_by(stripe_customer_id=customer_id)
                    .first()
                )
                if ba:
                    result.discrepancies.append(
                        Discrepancy(
                            category="unlinked_stripe_invoice",
                            severity="info",
                            billing_account_id=ba.id,
                            stripe_id=invoice_id,
                            detail=(
                                f"Stripe invoice {invoice_id} for customer "
                                f"{customer_id} (BA {ba.id}) has no matching "
                                f"Recharge — may be a Checkout payment tracked "
                                f"via PaymentIntent"
                            ),
                        ),
                    )


# ---------------------------------------------------------------------------
# Check 4: Credit balance integrity
# ---------------------------------------------------------------------------


def _check_credit_balance_integrity(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int = FIX_NONE,
) -> None:
    """For billing accounts with recent recharge activity, verify that
    the credit balance is plausible.

    Tier: disable autorecharge → safe.
    """
    # SUSPENDED with positive credits — flag only, suspension may be intentional
    suspended_positive = (
        session.query(BillingAccount)
        .filter(
            BillingAccount.account_status == "SUSPENDED",
            BillingAccount.credits > 0,
        )
        .all()
    )
    for ba in suspended_positive:
        result.discrepancies.append(
            Discrepancy(
                category="status_credit_mismatch",
                severity="warning",
                billing_account_id=ba.id,
                detail=(
                    f"BA {ba.id} is SUSPENDED but has positive credits "
                    f"(${float(ba.credits):.2f}) — may need manual review"
                ),
            ),
        )

    # Autorecharge enabled without Stripe customer → disable (safe)
    fix_autorecharge = fix_level >= FIX_SAFE
    phantom_autorecharge = (
        session.query(BillingAccount)
        .filter(
            BillingAccount.autorecharge.is_(True),
            BillingAccount.stripe_customer_id.is_(None),
        )
        .all()
    )
    for ba in phantom_autorecharge:
        result.discrepancies.append(
            Discrepancy(
                category="autorecharge_no_customer",
                severity="warning",
                billing_account_id=ba.id,
                detail=(
                    f"BA {ba.id} has autorecharge enabled but no "
                    f"stripe_customer_id — autorecharge will always fail"
                ),
                auto_fixed=fix_autorecharge,
            ),
        )
        if fix_autorecharge:
            ba.autorecharge = False
            logger.info(
                "Auto-fixed BA %s: disabled autorecharge (no stripe_customer_id)",
                ba.id,
            )


# ---------------------------------------------------------------------------
# Check 5: Stuck DISPUTED recharges
# ---------------------------------------------------------------------------


def _check_stuck_disputes(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int,
    stale_hours: int,
) -> None:
    """Find DISPUTED recharges older than ``stale_hours`` and verify the
    underlying Stripe dispute status.  A dispute that Stripe shows as
    *won* or *lost* but our DB still marks as DISPUTED indicates a
    missed ``charge.dispute.closed`` webhook.

    Tier: dispute lost → safe, dispute won (credits restored) → moderate.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)

    disputed: List[Recharge] = (
        session.query(Recharge)
        .filter(
            Recharge.status == RechargeStatus.DISPUTED,
            Recharge.at < cutoff,
        )
        .all()
    )

    result.disputes_checked += len(disputed)

    for recharge in disputed:
        if not recharge.stripe_invoice_id:
            age_hours = (
                datetime.now(timezone.utc) - recharge.at.replace(tzinfo=timezone.utc)
            ).total_seconds() / 3600
            result.discrepancies.append(
                Discrepancy(
                    category="stuck_dispute",
                    severity="warning",
                    billing_account_id=recharge.billing_account_id,
                    detail=(
                        f"Recharge {recharge.id} has been DISPUTED for "
                        f"{age_hours:.0f}h with no stripe_invoice_id"
                    ),
                ),
            )
            continue

        try:
            inv = stripe.Invoice.retrieve(recharge.stripe_invoice_id)
            charge_id = inv.get("charge")
            if not charge_id:
                result.discrepancies.append(
                    Discrepancy(
                        category="stuck_dispute",
                        severity="warning",
                        billing_account_id=recharge.billing_account_id,
                        stripe_id=recharge.stripe_invoice_id,
                        detail=(
                            f"Recharge {recharge.id} is DISPUTED but invoice "
                            f"{recharge.stripe_invoice_id} has no charge"
                        ),
                    ),
                )
                continue

            charge = stripe.Charge.retrieve(charge_id)
            dispute = (
                charge.get("dispute")
                if isinstance(charge, dict)
                else getattr(charge, "dispute", None)
            )

            if dispute is None:
                result.discrepancies.append(
                    Discrepancy(
                        category="stuck_dispute",
                        severity="warning",
                        billing_account_id=recharge.billing_account_id,
                        stripe_id=recharge.stripe_invoice_id,
                        detail=(
                            f"Recharge {recharge.id} is DISPUTED but charge "
                            f"{charge_id} has no dispute in Stripe"
                        ),
                    ),
                )
                continue

            dispute_status = (
                dispute.get("status")
                if isinstance(dispute, dict)
                else getattr(dispute, "status", None)
            )

            if dispute_status == "won":
                fix_won = fix_level >= FIX_MODERATE
                result.discrepancies.append(
                    Discrepancy(
                        category="stuck_dispute_resolved",
                        severity="critical",
                        billing_account_id=recharge.billing_account_id,
                        stripe_id=recharge.stripe_invoice_id,
                        detail=(
                            f"Recharge {recharge.id} is DISPUTED but Stripe "
                            f"dispute was won — missed charge.dispute.closed webhook"
                        ),
                        auto_fixed=fix_won,
                    ),
                )
                if fix_won:
                    recharge.status = RechargeStatus.PAID
                    ba = (
                        session.query(BillingAccount)
                        .filter_by(id=recharge.billing_account_id)
                        .with_for_update()
                        .first()
                    )
                    if ba:
                        ba.credits += recharge.quantity
                        if (
                            ba.account_status == "SUSPENDED"
                            and ba.suspension_reason != "admin_freeze"
                        ):
                            ba.account_status = "ACTIVE"
                            ba.suspension_reason = None
                        logger.info(
                            "Auto-fixed stuck dispute %s: DISPUTED → PAID, "
                            "re-credited %s to BA %s",
                            recharge.id,
                            recharge.quantity,
                            ba.id,
                        )

            elif dispute_status == "lost":
                fix_lost = fix_level >= FIX_SAFE
                result.discrepancies.append(
                    Discrepancy(
                        category="stuck_dispute_resolved",
                        severity="warning",
                        billing_account_id=recharge.billing_account_id,
                        stripe_id=recharge.stripe_invoice_id,
                        detail=(
                            f"Recharge {recharge.id} is DISPUTED but Stripe "
                            f"dispute was lost — status should be FAILED"
                        ),
                        auto_fixed=fix_lost,
                    ),
                )
                if fix_lost:
                    recharge.status = RechargeStatus.FAILED
                    logger.info(
                        "Auto-fixed stuck dispute %s: DISPUTED → FAILED "
                        "(dispute lost)",
                        recharge.id,
                    )

            # else: dispute still active (needs_response / under_review) — no action

        except stripe.InvalidRequestError:
            result.discrepancies.append(
                Discrepancy(
                    category="stuck_dispute",
                    severity="warning",
                    billing_account_id=recharge.billing_account_id,
                    stripe_id=recharge.stripe_invoice_id,
                    detail=(
                        f"Recharge {recharge.id} is DISPUTED and Stripe "
                        f"invoice {recharge.stripe_invoice_id} not found"
                    ),
                ),
            )
        except Exception as e:
            result.errors.append(
                f"Error checking dispute for recharge {recharge.id}: {e}",
            )


# ---------------------------------------------------------------------------
# Check 6: Duplicate Stripe customers
# ---------------------------------------------------------------------------


def _check_duplicate_stripe_customers(
    session: Session,
    result: ReconciliationResult,
) -> None:
    """Detect multiple billing accounts sharing the same
    ``stripe_customer_id``.  This indicates data corruption and can
    cause credits to be applied to the wrong account."""
    dupes = (
        session.query(
            BillingAccount.stripe_customer_id,
            func.count(BillingAccount.id).label("cnt"),
            func.array_agg(BillingAccount.id).label("ba_ids"),
        )
        .filter(BillingAccount.stripe_customer_id.isnot(None))
        .group_by(BillingAccount.stripe_customer_id)
        .having(func.count(BillingAccount.id) > 1)
        .all()
    )

    for row in dupes:
        result.discrepancies.append(
            Discrepancy(
                category="duplicate_stripe_customer",
                severity="critical",
                stripe_id=row.stripe_customer_id,
                detail=(
                    f"Stripe customer {row.stripe_customer_id} is linked to "
                    f"{row.cnt} billing accounts: {row.ba_ids}"
                ),
            ),
        )


# ---------------------------------------------------------------------------
# Check 7: Payment method health for autorecharge accounts
# ---------------------------------------------------------------------------


def _check_payment_methods(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int = FIX_NONE,
) -> None:
    """For accounts with autorecharge enabled and a Stripe customer,
    verify that at least one payment method exists.

    Tier: safe — disables autorecharge (it will fail anyway).
    """
    fix = fix_level >= FIX_SAFE
    accounts: List[BillingAccount] = (
        session.query(BillingAccount)
        .filter(
            BillingAccount.autorecharge.is_(True),
            BillingAccount.stripe_customer_id.isnot(None),
            BillingAccount.account_status == "ACTIVE",
        )
        .all()
    )

    for ba in accounts:
        try:
            methods = stripe.PaymentMethod.list(
                customer=ba.stripe_customer_id,
                limit=1,
            )
            has_method = bool(
                (
                    methods.get("data")
                    if isinstance(methods, dict)
                    else getattr(methods, "data", [])
                ),
            )
            if not has_method:
                result.discrepancies.append(
                    Discrepancy(
                        category="missing_payment_method",
                        severity="warning",
                        billing_account_id=ba.id,
                        stripe_id=ba.stripe_customer_id,
                        detail=(
                            f"BA {ba.id} has autorecharge enabled but Stripe "
                            f"customer {ba.stripe_customer_id} has no payment "
                            f"methods — autorecharge will fail"
                        ),
                        auto_fixed=fix,
                    ),
                )
                if fix:
                    ba.autorecharge = False
                    logger.info(
                        "Auto-fixed BA %s: disabled autorecharge "
                        "(no payment methods on customer %s)",
                        ba.id,
                        ba.stripe_customer_id,
                    )
        except Exception as e:
            result.errors.append(
                f"Error checking payment methods for BA {ba.id}: {e}",
            )


# ---------------------------------------------------------------------------
# Check 9: Credit balance ceiling
# ---------------------------------------------------------------------------


def _check_credit_balance_ceiling(
    session: Session,
    result: ReconciliationResult,
) -> None:
    """Verify that no billing account has more credits than the total
    amount ever recharged (sum of PAID recharges).  A balance exceeding
    this ceiling indicates phantom credit injection."""
    accounts: List[BillingAccount] = (
        session.query(BillingAccount).filter(BillingAccount.credits > 0).all()
    )

    for ba in accounts:
        total_recharged = (
            session.query(func.coalesce(func.sum(Recharge.quantity), 0))
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.status == RechargeStatus.PAID,
            )
            .scalar()
        )

        if ba.credits > total_recharged and total_recharged > 0:
            result.discrepancies.append(
                Discrepancy(
                    category="credit_exceeds_recharged",
                    severity="warning",
                    billing_account_id=ba.id,
                    detail=(
                        f"BA {ba.id} has credits ${float(ba.credits):.2f} but "
                        f"total PAID recharges are only "
                        f"${float(total_recharged):.2f}"
                    ),
                ),
            )


# ---------------------------------------------------------------------------
# Check 10: Webhook gap detection
# ---------------------------------------------------------------------------


def _check_webhook_gaps(
    session: Session,
    result: ReconciliationResult,
    *,
    lookback_days: int,
    fix_level: int = FIX_NONE,
) -> None:
    """Compare recent billing-relevant Stripe events against the
    ``WebhookLog`` table.  Missing entries indicate webhooks that
    were never delivered or processed.

    Tier: all — replays missed events through ``handle_event``.
    """
    lookback_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp(),
    )

    try:
        events = stripe.Event.list(created={"gte": lookback_ts}, limit=100)
    except Exception as e:
        result.errors.append(f"Failed to list Stripe events: {e}")
        return

    checked = 0
    max_events = 500

    for event in events.auto_paging_iter():
        event_type = (
            event.get("type", "")
            if isinstance(event, dict)
            else getattr(event, "type", "")
        )
        if event_type not in BILLING_EVENT_TYPES:
            continue

        checked += 1
        if checked > max_events:
            break

        event_id = (
            event.get("id") if isinstance(event, dict) else getattr(event, "id", "")
        )
        exists = (
            session.query(WebhookLog).filter_by(event_id=event_id).first() is not None
        )

        if not exists:
            fixed = False
            if fix_level >= FIX_ALL:
                try:
                    from orchestra.web.api.webhooks.stripe import handle_event

                    event_dict = (
                        dict(event)
                        if isinstance(event, dict)
                        else event.to_dict_recursive()
                    )
                    response = handle_event(event_dict)
                    status = getattr(response, "status_code", None)
                    if status and 200 <= status < 300:
                        fixed = True
                        logger.info(
                            "Auto-fixed missed webhook: replayed event "
                            "%s (%s), status=%s",
                            event_id,
                            event_type,
                            status,
                        )
                    else:
                        result.errors.append(
                            f"Replayed event {event_id} but got " f"status {status}",
                        )
                except Exception as e:
                    result.errors.append(
                        f"Failed to replay missed event {event_id}: {e}",
                    )

            result.discrepancies.append(
                Discrepancy(
                    category="missed_webhook",
                    severity="critical",
                    stripe_id=event_id,
                    detail=(
                        f"Stripe event {event_id} ({event_type}) was not "
                        f"found in WebhookLog — webhook may have been missed"
                    ),
                    auto_fixed=fixed,
                ),
            )

    result.events_checked += checked


# ---------------------------------------------------------------------------
# Check 11: Failed recharge credit void verification
# ---------------------------------------------------------------------------


def _check_failed_recharge_voids(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int = FIX_NONE,
) -> None:
    """Verify that FAILED auto-recharges had their credits properly voided.

    Auto-recharge grants credits immediately (PENDING_INVOICE) and bills
    at month-end.  When the invoice ultimately fails, credits should be
    voided (deducted back).  If the void was missed, the account has
    unearned credits.

    Only examines recharges that were already FAILED before this
    reconciliation run (excludes recharges just fixed by earlier checks
    like stale-recharge → FAILED, which void credits as part of their
    own auto-fix).

    Tier: ``all`` — deducts the unvoided credits.
    """
    already_fixed_ids = {
        d.stripe_id for d in result.discrepancies if d.auto_fixed and d.stripe_id
    }

    failed_auto = (
        session.query(Recharge)
        .filter(
            Recharge.status == RechargeStatus.FAILED,
            Recharge.type == RECHARGE_TYPE_AUTO,
            Recharge.stripe_invoice_id.isnot(None),
            Recharge.quantity > 0,
        )
        .all()
    )

    failed_auto = [
        r for r in failed_auto if r.stripe_invoice_id not in already_fixed_ids
    ]

    for recharge in failed_auto:
        try:
            inv = stripe.Invoice.retrieve(recharge.stripe_invoice_id)
        except Exception:
            continue

        inv_status = (
            inv.get("status") if isinstance(inv, dict) else getattr(inv, "status", None)
        )
        if inv_status not in ("void", "uncollectible"):
            continue

        ba = (
            session.query(BillingAccount)
            .filter_by(id=recharge.billing_account_id)
            .first()
        )
        if ba is None:
            continue

        paid_total = (
            session.query(func.coalesce(func.sum(Recharge.quantity), 0))
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.status == RechargeStatus.PAID,
            )
            .scalar()
        )

        if ba.credits > paid_total:
            fixed = False
            if fix_level >= FIX_ALL:
                from orchestra.db.dao.billing_account_dao import BillingAccountDAO

                ba_dao = BillingAccountDAO(session)
                ba_dao.deduct_credits(ba.id, float(recharge.quantity))
                fixed = True
                logger.info(
                    "Auto-fixed BA %s: voided %s unearned credits from "
                    "FAILED recharge %s",
                    ba.id,
                    recharge.quantity,
                    recharge.id,
                )

            result.discrepancies.append(
                Discrepancy(
                    category="unvoided_failed_recharge",
                    severity="critical",
                    billing_account_id=ba.id,
                    stripe_id=recharge.stripe_invoice_id,
                    detail=(
                        f"FAILED auto-recharge {recharge.id} "
                        f"(${float(recharge.quantity):.2f}) was not voided — "
                        f"BA has {float(ba.credits):.2f} credits vs "
                        f"{float(paid_total):.2f} from paid recharges"
                    ),
                    auto_fixed=fixed,
                ),
            )

        result.recharges_checked += 1


# ---------------------------------------------------------------------------
# Check 12: Grace period without negative balance
# ---------------------------------------------------------------------------


def _check_orphaned_grace_periods(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int = FIX_NONE,
) -> None:
    """Flag contacts stuck in ``grace_period`` whose billing account
    has non-negative credits.

    The daily suspension routine should restore these automatically, but
    if it isn't running (or failed), contacts can be stuck indefinitely
    in grace_period even though the account can afford them.

    Tier: ``safe`` — restores contacts to ``active`` (no financial impact,
    the account has sufficient credits).
    """
    from orchestra.db.models.orchestra_models import AssistantContact

    grace_contacts = (
        session.query(AssistantContact)
        .filter(AssistantContact.status == "grace_period")
        .all()
    )

    if not grace_contacts:
        return

    ba_ids = {c.assistant.user_id for c in grace_contacts if c.assistant}
    ba_cache: Dict[int, BillingAccount] = {}

    for contact in grace_contacts:
        if not contact.assistant:
            continue

        assistant = contact.assistant
        ba = ba_cache.get(id(assistant))
        if ba is None:
            from orchestra.routines.assistant_contact_levy import (
                _get_billing_account_for_assistant,
            )

            ba = _get_billing_account_for_assistant(session, assistant)
            if ba is None:
                continue
            ba_cache[id(assistant)] = ba

        if ba.credits >= 0:
            fixed = False
            if fix_level >= FIX_SAFE:
                contact.status = "active"
                contact.grace_period_started_at = None
                fixed = True
                logger.info(
                    "Auto-fixed contact %s: restored from grace_period → "
                    "active (BA %s has credits %.2f)",
                    contact.id,
                    ba.id,
                    float(ba.credits),
                )

            result.discrepancies.append(
                Discrepancy(
                    category="orphaned_grace_period",
                    severity="warning",
                    billing_account_id=ba.id,
                    detail=(
                        f"Contact {contact.id} ({contact.contact_type}) is in "
                        f"grace_period but BA {ba.id} has "
                        f"${float(ba.credits):.2f} credits — "
                        f"suspension routine may not be running"
                    ),
                    auto_fixed=fixed,
                ),
            )


# ---------------------------------------------------------------------------
# Check 14: SUSPENDED without dispute justification
# ---------------------------------------------------------------------------


def _check_unjustified_suspensions(
    session: Session,
    result: ReconciliationResult,
    *,
    fix_level: int = FIX_NONE,
) -> None:
    """Flag SUSPENDED accounts whose suspension may no longer be justified.

    Uses ``suspension_reason`` to decide whether a suspension is intentional:

    - ``admin_freeze`` → intentional, always skipped.
    - ``dispute`` → flagged only if no active DISPUTED recharges remain
      (dispute may have been resolved without the webhook clearing the
      status).  Auto-fixed at ``moderate``.
    - ``None`` (legacy, pre-``suspension_reason``) → flagged; auto-fixed
      at ``moderate`` since no explicit reason exists.

    Tier: ``moderate`` — restores SUSPENDED → ACTIVE when there are no
    active disputes and suspension_reason is ``dispute`` or ``None``.
    """
    suspended = (
        session.query(BillingAccount)
        .filter(BillingAccount.account_status == "SUSPENDED")
        .all()
    )

    for ba in suspended:
        if ba.suspension_reason == "admin_freeze":
            continue

        active_disputes = (
            session.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.status == RechargeStatus.DISPUTED,
            )
            .count()
        )

        if active_disputes > 0:
            continue

        fixed = False
        if fix_level >= FIX_MODERATE:
            ba.account_status = "ACTIVE"
            ba.suspension_reason = None
            fixed = True
            logger.info(
                "Auto-fixed BA %s: restored SUSPENDED → ACTIVE "
                "(reason=%r, no active disputes)",
                ba.id,
                ba.suspension_reason,
            )

        if ba.suspension_reason == "dispute":
            severity = "info"
            detail_suffix = " — dispute reason set, but no active disputes remain"
        else:
            severity = "warning"
            detail_suffix = (
                " — no suspension reason, likely leftover from old billing guard"
            )

        result.discrepancies.append(
            Discrepancy(
                category="unjustified_suspension",
                severity=severity,
                billing_account_id=ba.id,
                detail=(
                    f"BA {ba.id} is SUSPENDED with no active DISPUTED "
                    f"recharges (credits: ${float(ba.credits):.2f})"
                    f"{detail_suffix}"
                ),
                auto_fixed=fixed,
            ),
        )

    result.accounts_checked += len(suspended)


# ---------------------------------------------------------------------------
# Post-collection enrichment
# ---------------------------------------------------------------------------

_STRIPE_DASHBOARD_PREFIXES = {
    "cus_": ("customers", "customers"),
    "in_": ("invoices", "invoices"),
    "ch_": ("payments", "payments"),
    "dp_": ("disputes", "disputes"),
    "evt_": ("events", "events"),
    "pi_": ("payments", "payments"),
    "sub_": ("subscriptions", "subscriptions"),
}


def _stripe_dashboard_url(stripe_id: Optional[str], mode: str) -> Optional[str]:
    """Build a direct Stripe Dashboard URL for a given Stripe object ID."""
    if not stripe_id:
        return None
    base = "https://dashboard.stripe.com"
    if mode == "test":
        base += "/test"
    for prefix, (section, _) in _STRIPE_DASHBOARD_PREFIXES.items():
        if stripe_id.startswith(prefix):
            return f"{base}/{section}/{stripe_id}"
    return None


def _enrich_discrepancies(
    session: Session,
    result: ReconciliationResult,
) -> None:
    """Populate owner identity, Stripe URLs, and recharge context on every
    discrepancy after all checks have run.  Uses bulk queries to avoid N+1."""

    ba_ids = {
        d.billing_account_id
        for d in result.discrepancies
        if d.billing_account_id is not None
    }
    if not ba_ids:
        return

    # --- Owner lookup (single query per entity type) ----------------------
    owner_map: Dict[int, tuple] = {}  # ba_id -> (type, email, name)

    user_rows = (
        session.query(User.billing_account_id, User.email, User.name)
        .filter(User.billing_account_id.in_(ba_ids))
        .all()
    )
    for ba_id, email, name in user_rows:
        owner_map[ba_id] = ("user", email, name)

    org_rows = (
        session.query(
            Organization.billing_account_id,
            Organization.name,
            User.email,
        )
        .join(User, Organization.owner_id == User.id)
        .filter(Organization.billing_account_id.in_(ba_ids))
        .all()
    )
    for ba_id, org_name, owner_email in org_rows:
        owner_map[ba_id] = ("org", owner_email, org_name)

    # --- Recharge context for credit-related discrepancies ----------------
    credit_categories = frozenset(
        {
            "credit_balance_integrity",
            "credit_balance_ceiling",
            "orphaned_stripe_invoice",
            "stale_recharge",
            "stuck_dispute",
            "status_credit_mismatch",
            "autorecharge_no_customer",
            "unvoided_failed_recharge",
            "unjustified_suspension",
        },
    )
    context_ba_ids = {
        d.billing_account_id
        for d in result.discrepancies
        if d.billing_account_id is not None and d.category in credit_categories
    }

    recharge_map: Dict[int, List[Dict]] = {}
    if context_ba_ids:
        recent_recharges = (
            session.query(Recharge)
            .filter(Recharge.billing_account_id.in_(context_ba_ids))
            .order_by(Recharge.at.desc())
            .all()
        )
        for r in recent_recharges:
            ba_list = recharge_map.setdefault(r.billing_account_id, [])
            if len(ba_list) < 5:
                ba_list.append(
                    {
                        "id": r.id,
                        "at": r.at.isoformat() if r.at else None,
                        "status": (
                            r.status if isinstance(r.status, str) else r.status.value
                        ),
                        "amount_usd": float(r.amount_usd),
                        "quantity": float(r.quantity),
                        "type": r.type,
                        "stripe_invoice_id": r.stripe_invoice_id,
                    },
                )

    # --- Apply to each discrepancy ----------------------------------------
    for d in result.discrepancies:
        if d.billing_account_id and d.billing_account_id in owner_map:
            d.owner_type, d.owner_email, d.owner_name = owner_map[d.billing_account_id]

        d.stripe_url = _stripe_dashboard_url(d.stripe_id, result.stripe_mode)

        if d.billing_account_id and d.category in credit_categories:
            d.recharge_context = recharge_map.get(d.billing_account_id)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_summary(result: ReconciliationResult) -> None:
    """Emit structured log lines for the reconciliation run."""
    summary = {
        "message": "Billing reconciliation complete",
        "stripe_mode": result.stripe_mode,
        "accounts_checked": result.accounts_checked,
        "recharges_checked": result.recharges_checked,
        "invoices_checked": result.invoices_checked,
        "total_discrepancies": len(result.discrepancies),
        "critical": result.critical_count,
        "warnings": result.warning_count,
        "auto_fixed": result.auto_fixed_count,
        "errors": len(result.errors),
    }

    if result.critical_count > 0:
        logger.error(summary)
    elif result.warning_count > 0:
        logger.warning(summary)
    else:
        logger.info(summary)

    for d in result.discrepancies:
        log_fn = logger.error if d.severity == "critical" else logger.warning
        log_fn(
            {
                "message": "Reconciliation discrepancy",
                "category": d.category,
                "severity": d.severity,
                "billing_account_id": d.billing_account_id,
                "stripe_id": d.stripe_id,
                "detail": d.detail,
                "auto_fixed": d.auto_fixed,
            },
        )
