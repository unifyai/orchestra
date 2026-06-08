"""Expiring credit-grant ledger helpers (self-serve subscription model).

Credit grants carry an *expiry* so the platform can enforce the
self-serve rules without any new per-bucket balance columns — the
expiry lives in ``credit_transaction.detail`` (jsonb) and the
unconsumed remainder of an expired grant is *inferred from the ledger*:

    detail = {"grant_kind": "trial" | "plan", "expires_at": "<ISO-8601 UTC>"}

Two grant kinds expire:

* ``trial`` — the one-time signup grant (``settings.signup_credit_grant``);
  expires 1 week after signup.
* ``plan`` — the monthly subscription credit grant; expires at the next
  billing anniversary (credits reset each cycle, unused credits forfeit).

Migrated / promotional / refund / dispute credits have **no** expiry
(``grant_kind`` absent) and never forfeit.

Forfeiting posts a negative ``credit_transaction`` with ``category =
"forfeit"`` and a ``detail.forfeits`` list pinning exactly which grant
rows (and how much of each) were forfeited. That makes the computation
**idempotent**: a re-run sees the prior forfeit, attributes it back to
the same grant rows, and finds nothing left to forfeit.

Consumption is attributed **soonest-expiry-first** (non-expiring credits
are consumed last and never forfeited). In the common case — a fresh
account whose only credit is the trial grant — this reduces to "zero the
balance at expiry".
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import BillingAccount, CreditTransaction

logger = logging.getLogger(__name__)

# Ledger category for the negative forfeit transaction.
FORFEIT_CATEGORY = "forfeit"

# Grant kinds that carry an expiry.
GRANT_KIND_TRIAL = "trial"
GRANT_KIND_PLAN = "plan"

# Ledger category per grant kind. The ``plan`` (subscription) grant is *paid*
# — the customer pays the subscription invoice that funds it — so it records
# as ``subscription_recharge`` alongside one-off ``recharge`` purchases, while
# ``trial``/goodwill credits are free and record as ``grant``. (The expiry /
# forfeit logic keys off ``detail.grant_kind``, never this category string, so
# the split is purely for ledger reporting.)
CATEGORY_SUBSCRIPTION_RECHARGE = "subscription_recharge"
CATEGORY_GRANT = "grant"
_GRANT_KIND_TO_CATEGORY = {
    GRANT_KIND_PLAN: CATEGORY_SUBSCRIPTION_RECHARGE,
    GRANT_KIND_TRIAL: CATEGORY_GRANT,
}


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def add_one_month(reference: datetime) -> datetime:
    """Return ``reference`` advanced by one calendar month (anniversary).

    Clamps the day to the target month's length (e.g. Jan 31 -> Feb 28/29)
    so the cycle stays anniversary-anchored without overflowing.
    """
    ref = _utc(reference)
    year = ref.year + (1 if ref.month == 12 else 0)
    month = 1 if ref.month == 12 else ref.month + 1
    last_day = calendar.monthrange(year, month)[1]
    return ref.replace(year=year, month=month, day=min(ref.day, last_day))


def add_one_year(reference: datetime) -> datetime:
    """Return ``reference`` advanced by one year (annual anniversary).

    Clamps Feb-29 to Feb-28 on a non-leap target year so the annual cycle
    stays anchored without overflowing.
    """
    ref = _utc(reference)
    year = ref.year + 1
    last_day = calendar.monthrange(year, ref.month)[1]
    return ref.replace(year=year, day=min(ref.day, last_day))


def subtract_one_month(reference: datetime) -> datetime:
    """Return ``reference`` moved back one calendar month (anniversary).

    Inverse of :func:`add_one_month`; clamps the day to the target
    month's length so the cycle stays anchored without overflowing.
    """
    ref = _utc(reference)
    year = ref.year - (1 if ref.month == 1 else 0)
    month = 12 if ref.month == 1 else ref.month - 1
    last_day = calendar.monthrange(year, month)[1]
    return ref.replace(year=year, month=month, day=min(ref.day, last_day))


def subtract_one_year(reference: datetime) -> datetime:
    """Return ``reference`` moved back one year (annual anniversary)."""
    ref = _utc(reference)
    year = ref.year - 1
    last_day = calendar.monthrange(year, ref.month)[1]
    return ref.replace(year=year, day=min(ref.day, last_day))


def remaining_period_fraction(
    period_end: Optional[datetime],
    now: datetime,
    *,
    annual: bool,
) -> Decimal:
    """Fraction of the current billing period still remaining at ``now``.

    The period runs from one interval before ``period_end`` (the Stripe
    ``current_period_start`` for a clean cycle) up to ``period_end``. Used
    to prorate a mid-cycle upgrade credit grant so the credits handed out
    immediately match Stripe's prorated charge for the remainder of the
    period; the rest of the tier lands at the next cycle's ``invoice.paid``.

    Returns ``1`` when ``period_end`` is unknown (preserves the legacy
    full-delta grant for accounts with no recorded cycle end) and ``0``
    once the period has elapsed. Result is clamped to ``[0, 1]``.
    """
    if period_end is None:
        return Decimal("1")
    end = _utc(period_end)
    moment = _utc(now)
    if end <= moment:
        return Decimal("0")
    start = subtract_one_year(end) if annual else subtract_one_month(end)
    total = (end - start).total_seconds()
    if total <= 0:
        return Decimal("1")
    remaining = (end - moment).total_seconds()
    fraction = Decimal(str(remaining)) / Decimal(str(total))
    if fraction <= 0:
        return Decimal("0")
    if fraction >= 1:
        return Decimal("1")
    return fraction


def parse_expiry(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 expiry string from ``detail`` into aware UTC."""
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        logger.warning("Unparseable grant expires_at: %r", value)
        return None


@dataclass
class GrantLot:
    """One expiring grant row and its inferred unconsumed remainder."""

    txn_id: int
    grant_kind: str
    expires_at: datetime
    original: Decimal
    at: datetime
    remaining: Decimal


def compute_grant_lots(session: Session, billing_account_id: int) -> List[GrantLot]:
    """Reconstruct each expiring grant's unconsumed remainder from the ledger.

    Algorithm:
      1. Collect every expiring grant (positive txn tagged ``grant_kind`` +
         ``expires_at``) as a lot; lump non-expiring positive credits into a
         single untracked tail (consumed last, never forfeited).
      2. Apply *targeted* forfeits (``category='forfeit'`` rows whose
         ``detail.forfeits`` pin specific grant txn ids) directly back to
         their lots — this is what makes the whole thing idempotent.
      3. Allocate all remaining debits (usage, voids, untargeted forfeits)
         **soonest-expiry-first** across the lots; leftover spills onto the
         non-expiring tail (untracked) and is ignored.
    """
    txns = list(
        session.execute(
            select(CreditTransaction)
            .where(CreditTransaction.billing_account_id == billing_account_id)
            .order_by(CreditTransaction.at.asc(), CreditTransaction.id.asc()),
        )
        .scalars()
        .all(),
    )

    lots: List[GrantLot] = []
    targeted_forfeits: dict[int, Decimal] = {}
    usage_total = Decimal("0")

    for t in txns:
        detail = t.detail or {}
        if t.amount > 0:
            grant_kind = detail.get("grant_kind")
            expires_at = parse_expiry(detail.get("expires_at"))
            if grant_kind and expires_at is not None:
                lots.append(
                    GrantLot(
                        txn_id=t.id,
                        grant_kind=grant_kind,
                        expires_at=expires_at,
                        original=Decimal(str(t.amount)),
                        at=_utc(t.at),
                        remaining=Decimal(str(t.amount)),
                    ),
                )
            # else: non-expiring credit — untracked tail.
        elif t.amount < 0:
            forfeits = detail.get("forfeits")
            if t.category == FORFEIT_CATEGORY and forfeits:
                for f in forfeits:
                    try:
                        tid = int(f["grant_txn_id"])
                        targeted_forfeits[tid] = targeted_forfeits.get(
                            tid,
                            Decimal("0"),
                        ) + Decimal(str(f["amount"]))
                    except (KeyError, ValueError, TypeError):
                        continue
            else:
                usage_total += -Decimal(str(t.amount))

    # Step 2 — apply targeted forfeits back to their lots.
    for lot in lots:
        forfeited = targeted_forfeits.get(lot.txn_id, Decimal("0"))
        lot.remaining = max(Decimal("0"), lot.original - forfeited)

    # Step 3 — allocate usage soonest-expiry-first.
    pool = usage_total
    for lot in sorted(lots, key=lambda x: (x.expires_at, x.at, x.txn_id)):
        if pool <= 0:
            break
        take = min(pool, lot.remaining)
        lot.remaining -= take
        pool -= take

    return lots


def _post_forfeit(
    session: Session,
    billing_account: BillingAccount,
    lots_to_forfeit: List[GrantLot],
    *,
    reason: str,
) -> Decimal:
    """Post a single negative ``forfeit`` txn for the given lots.

    Caps the forfeit at the current wallet balance and never goes below
    zero; never touches non-expiring credits. Returns the amount forfeited.
    """
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    total = sum((lot.remaining for lot in lots_to_forfeit), Decimal("0"))
    if total <= 0:
        return Decimal("0")

    # Defensive cap — never drive the wallet negative on a forfeit.
    balance = Decimal(str(billing_account.credits or 0))
    if balance <= 0:
        return Decimal("0")
    capped = min(total, balance)

    forfeits_detail = [
        {"grant_txn_id": lot.txn_id, "amount": str(lot.remaining)}
        for lot in lots_to_forfeit
        if lot.remaining > 0
    ]

    BillingAccountDAO(session).deduct_credits(
        billing_account.id,
        float(capped),
        category=FORFEIT_CATEGORY,
        description=f"Forfeited unconsumed expiring credits ({reason})",
        detail={"reason": reason, "forfeits": forfeits_detail},
    )
    logger.info(
        {
            "message": "Forfeited expiring credit grant remainder",
            "billing_account_id": billing_account.id,
            "reason": reason,
            "amount": float(capped),
            "grant_txn_ids": [lot.txn_id for lot in lots_to_forfeit],
        },
    )
    return capped


def forfeit_expired_grants(
    session: Session,
    billing_account: BillingAccount,
    *,
    now: Optional[datetime] = None,
) -> Decimal:
    """Forfeit the unconsumed remainder of every *expired* grant.

    Used by the scheduled sweep. Only forfeits lots whose ``expires_at``
    is at or before ``now``.
    """
    moment = _utc(now) if now is not None else datetime.now(timezone.utc)
    lots = compute_grant_lots(session, billing_account.id)
    expired = [
        lot for lot in lots if lot.remaining > 0 and lot.expires_at <= moment
    ]
    if not expired:
        return Decimal("0")
    return _post_forfeit(session, billing_account, expired, reason="grant_expiry")


def forfeit_plan_grant_remainder(
    session: Session,
    billing_account: BillingAccount,
) -> Decimal:
    """Forfeit the unconsumed remainder of all ``plan`` grants (forced).

    Called on the subscription cycle boundary before granting the fresh
    monthly allowance: credits reset each cycle, so any leftover from the
    prior plan grant forfeits regardless of its nominal ``expires_at``.
    """
    lots = compute_grant_lots(session, billing_account.id)
    plan_lots = [
        lot
        for lot in lots
        if lot.remaining > 0 and lot.grant_kind == GRANT_KIND_PLAN
    ]
    if not plan_lots:
        return Decimal("0")
    return _post_forfeit(
        session,
        billing_account,
        plan_lots,
        reason="plan_cycle_reset",
    )


def grant_expiring_credits(
    session: Session,
    billing_account_id: int,
    amount: Decimal | float,
    *,
    grant_kind: str,
    expires_at: datetime,
    description: Optional[str] = None,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
) -> Optional[Decimal]:
    """Add a credit grant tagged with an expiry to the ledger + wallet.

    Thin wrapper over ``BillingAccountDAO.add_credits`` that stamps the
    ``grant_kind`` / ``expires_at`` into ``detail`` so the forfeit logic
    can find it. Returns the new wallet balance (CREDITS mode).
    """
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    return BillingAccountDAO(session).add_credits(
        billing_account_id,
        float(amount),
        category=_GRANT_KIND_TO_CATEGORY.get(grant_kind, CATEGORY_GRANT),
        user_id=user_id,
        organization_id=organization_id,
        description=description or f"{grant_kind} credit grant",
        detail={
            "grant_kind": grant_kind,
            "expires_at": _utc(expires_at).isoformat(),
        },
    )


def signup_trial_expiry(reference: Optional[datetime] = None) -> datetime:
    """Trial grants expire one week after signup."""
    moment = _utc(reference) if reference is not None else datetime.now(timezone.utc)
    return moment + timedelta(days=7)


def format_display_credits(usd_value: Decimal | float | int) -> str:
    """Format a canonical USD value as a customer-facing credit count.

    Mirrors the console's display-only framing (``DISPLAY_CREDITS_PER_USD``):
    the wallet denominates in USD value (1 unit = $1) but customers are
    shown *credits* = USD × multiplier. Used by outbound emails so the
    numbers customers receive match what they see in the console. Never
    used for settlement.
    """
    from orchestra.settings import settings

    credits = round(float(usd_value) * settings.display_credits_per_usd)
    return f"{credits:,}"
