"""Freeze sweeps backing the card-gated trial rollout.

Two admin-triggered sweeps, both idempotent and dry-run-first:

* :func:`freeze_never_paid_accounts` — one-shot migration sweep for the
  card gate: every ACTIVE account with neither a linked subscription nor
  any real payment history is suspended with reason ``card_required``.
  Orgs holding an admin-granted free trial are exempt, so a comped
  white-glove account is not frozen out from under the customer.
  Completing the trial Checkout auto-reinstates the account
  (``trial_subscription.apply_trial_checkout_completed``).

* :func:`freeze_abuse_fingerprints` — recurring guard against free-credit
  extraction. The observed farming signature is crisp: a young,
  never-paid account whose LLM spend flows almost entirely through the
  raw API channel (ledger rows with ``assistant_id`` NULL — the OSS CLI
  path, not the product) and whose wallet is near or below zero.
  Matching accounts are suspended with reason ``abuse_fingerprint``.
  Comped orgs are exempt: the grant opens the raw API to them and never
  tops their wallet back up, so a white-glove evaluation driving the CLI
  arrives at precisely the state this signature keys on.

* :func:`freeze_burner_clusters` — successor signal for when free credits
  are Console-only. Closing the API to never-paid accounts does not just
  reduce farming, it *moves* it: the raw-API channel the fingerprint
  above keys on becomes unreachable, so that sweep quietly stops finding
  anything, and whatever farming continues does so through the Console
  where ``assistant_id`` is populated and looks like ordinary use.

  No single-account signal separates the two there. Burn velocity is the
  tempting one and it is wrong — an enthusiastic evaluator's first
  session drains a grant just as fast as a script, and freezing them is
  the worst outcome available. What does separate them is repetition
  across accounts, so this sweep only ever acts on a *cluster*: several
  never-paid accounts sharing a signup origin inside a short window,
  each having drained its grant.

All three return a summary dict suitable for the admin-endpoint response
and Cloud Scheduler run logs.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from orchestra.db.models.enums import RECHARGE_TYPE_PAYMENT
from orchestra.db.models.orchestra_models import (
    BillingAccount,
    CreditTransaction,
    Organization,
    Recharge,
    RechargeStatus,
    User,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# Abuse-fingerprint thresholds. Deliberately conservative: the sweep
# freezes only accounts whose usage is overwhelmingly raw-API and whose
# trial value is already extracted — a genuine product evaluator (Twin
# chats populate ``assistant_id``) never matches.
ABUSE_LOOKBACK_DAYS = 14
ABUSE_MIN_LLM_SPEND = 20.0
ABUSE_NULL_ASSISTANT_FRACTION = 0.9
ABUSE_MAX_REMAINING_CREDITS = 5.0


@dataclass
class SweepResult:
    """Summary of one sweep run."""

    dry_run: bool = True
    scanned: int = 0
    frozen: int = 0
    billing_account_ids: List[int] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _paid_ba_ids(session: Session) -> set[int]:
    """Billing accounts with any real (non-promo) payment history."""
    rows = session.execute(
        select(Recharge.billing_account_id)
        .where(
            Recharge.type.in_((RECHARGE_TYPE_PAYMENT, "auto")),
            Recharge.status == RechargeStatus.PAID,
        )
        .distinct(),
    ).fetchall()
    return {row[0] for row in rows}


def _free_trial_ba_ids(session: Session) -> set[int]:
    """Billing accounts of orgs holding an admin-granted free trial."""
    rows = session.execute(
        select(Organization.billing_account_id)
        .where(
            Organization.free_trial.is_(True),
            Organization.billing_account_id.isnot(None),
        )
        .distinct(),
    ).fetchall()
    return {row[0] for row in rows}


def freeze_never_paid_accounts(
    session: Session,
    *,
    dry_run: bool = True,
) -> SweepResult:
    """Suspend every ACTIVE, never-paid, unsubscribed billing account."""
    result = SweepResult(dry_run=dry_run)

    if not settings.require_card_on_file:
        result.note = (
            "require_card_on_file is disabled — refusing to freeze; "
            "enable the flag before running this sweep."
        )
        return result

    paid = _paid_ba_ids(session)
    comped = _free_trial_ba_ids(session)
    candidates = session.execute(
        select(BillingAccount).where(
            BillingAccount.account_status == "ACTIVE",
            BillingAccount.stripe_subscription_id.is_(None),
        ),
    ).scalars()

    for ba in candidates:
        result.scanned += 1
        if ba.id in paid or ba.id in comped:
            continue
        result.billing_account_ids.append(ba.id)
        if not dry_run:
            ba.account_status = "SUSPENDED"
            ba.suspension_reason = "card_required"
    result.frozen = len(result.billing_account_ids)

    if not dry_run:
        session.flush()
    logger.info(
        {
            "message": "Card-gate freeze sweep finished",
            **result.to_dict(),
            # Don't spam the log payload with every id.
            "billing_account_ids": result.billing_account_ids[:50],
        },
    )
    return result


def freeze_abuse_fingerprints(
    session: Session,
    *,
    dry_run: bool = True,
) -> SweepResult:
    """Suspend never-paid accounts matching the credit-farming signature."""
    result = SweepResult(dry_run=dry_run)
    cutoff = datetime.now(timezone.utc) - timedelta(days=ABUSE_LOOKBACK_DAYS)

    spend_rows = session.execute(
        select(
            CreditTransaction.billing_account_id,
            func.sum(-CreditTransaction.amount).label("spend"),
            func.sum(
                cast(CreditTransaction.assistant_id.is_(None), Integer),
            ).label("null_rows"),
            func.count().label("rows"),
        )
        .where(
            CreditTransaction.category == "llm",
            CreditTransaction.at >= cutoff,
        )
        .group_by(CreditTransaction.billing_account_id),
    ).fetchall()

    paid = _paid_ba_ids(session)
    comped = _free_trial_ba_ids(session)

    for ba_id, spend, null_rows, total_rows in spend_rows:
        result.scanned += 1
        if ba_id in paid or ba_id in comped:
            continue
        if float(spend or 0) < ABUSE_MIN_LLM_SPEND:
            continue
        if total_rows == 0 or (null_rows / total_rows) < ABUSE_NULL_ASSISTANT_FRACTION:
            continue
        ba = session.get(BillingAccount, ba_id)
        if ba is None or ba.account_status != "ACTIVE":
            continue
        if float(ba.credits) > ABUSE_MAX_REMAINING_CREDITS:
            continue
        result.billing_account_ids.append(ba_id)
        if not dry_run:
            ba.account_status = "SUSPENDED"
            ba.suspension_reason = "abuse_fingerprint"

    result.frozen = len(result.billing_account_ids)
    if not dry_run:
        session.flush()
    logger.warning(
        {
            "message": "Abuse-fingerprint freeze sweep finished",
            **result.to_dict(),
        },
    )
    return result


def _drained_never_paid_ba_ids(session: Session) -> set[int]:
    """Billing accounts that are never-paid and have spent their grant.

    "Spent" rather than "spending": an account still holding most of its
    credits has extracted nothing, and freezing it on origin alone would
    punish colleagues for sharing an office IP.
    """
    paid = _paid_ba_ids(session)
    comped = _free_trial_ba_ids(session)

    spend_rows = session.execute(
        select(
            CreditTransaction.billing_account_id,
            func.sum(-CreditTransaction.amount).label("spend"),
        )
        .where(CreditTransaction.category == "llm")
        .group_by(CreditTransaction.billing_account_id),
    ).fetchall()

    drained: set[int] = set()
    for ba_id, spend in spend_rows:
        if ba_id is None or ba_id in paid or ba_id in comped:
            continue
        if float(spend or 0) < settings.burner_cluster_min_llm_spend:
            continue
        ba = session.get(BillingAccount, ba_id)
        if ba is None or ba.account_status != "ACTIVE":
            continue
        if float(ba.credits) > settings.burner_cluster_max_remaining_credits:
            continue
        drained.add(ba_id)
    return drained


def freeze_burner_clusters(
    session: Session,
    *,
    dry_run: bool = True,
) -> SweepResult:
    """Suspend never-paid accounts that farmed a grant in an origin cluster.

    An account is frozen only when *all* of the following hold, because
    each condition alone has an innocent explanation:

    * it shares a signup IP or user-agent hash with enough other accounts
      (``burner_cluster_min_accounts``) — but an office, a VPN exit or a
      university NAT does that legitimately;
    * those signups landed inside ``burner_cluster_window_days`` — but a
      team onboarding together does that legitimately;
    * every account in the cluster has never paid and has drained its
      grant — which, together with the above, is not something a real
      team does.

    Accounts with no recorded provenance are skipped entirely rather than
    grouped under a shared ``NULL``: lumping them together would invent a
    cluster out of missing data, which is precisely the failure mode this
    sweep must not have.
    """
    result = SweepResult(dry_run=dry_run)
    # ``User.created_at`` is TIMESTAMP *without* time zone (unlike
    # ``CreditTransaction.at``), so compare it against a naive UTC value
    # rather than letting the driver coerce an aware one against the
    # server's session timezone.
    window_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=settings.burner_cluster_window_days,
    )

    drained = _drained_never_paid_ba_ids(session)
    if not drained:
        result.note = "No drained never-paid accounts to correlate."
        return result

    users = (
        session.execute(
            select(User).where(
                User.billing_account_id.in_(drained),
                User.created_at >= window_start,
            ),
        )
        .scalars()
        .all()
    )

    clusters: dict[tuple[str, str], list[User]] = {}
    for user in users:
        for kind, value in (
            ("ip", user.signup_ip),
            ("ua", user.signup_user_agent_hash),
        ):
            if not value:
                continue
            clusters.setdefault((kind, value), []).append(user)

    flagged_ba_ids: set[int] = set()
    for (kind, value), members in clusters.items():
        result.scanned += 1
        member_ba_ids = {u.billing_account_id for u in members if u.billing_account_id}
        if len(member_ba_ids) < settings.burner_cluster_min_accounts:
            continue
        logger.warning(
            {
                "message": "Burner cluster matched",
                "origin_kind": kind,
                "accounts": len(member_ba_ids),
            },
        )
        flagged_ba_ids |= member_ba_ids

    for ba_id in sorted(flagged_ba_ids):
        ba = session.get(BillingAccount, ba_id)
        if ba is None or ba.account_status != "ACTIVE":
            continue
        result.billing_account_ids.append(ba_id)
        if not dry_run:
            ba.account_status = "SUSPENDED"
            ba.suspension_reason = "abuse_fingerprint"

    result.frozen = len(result.billing_account_ids)
    if not dry_run:
        session.flush()
    logger.warning(
        {
            "message": "Burner-cluster freeze sweep finished",
            **result.to_dict(),
            "billing_account_ids": result.billing_account_ids[:50],
        },
    )
    return result
