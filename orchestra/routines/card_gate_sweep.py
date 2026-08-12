"""Billing sweeps: one that suspends, one that only ever reports.

* :func:`freeze_never_paid_accounts` — one-shot migration sweep for the
  card gate: every ACTIVE account with neither a linked subscription nor
  any real payment history is suspended with reason ``card_required``.
  Orgs holding an admin-granted free trial are exempt, so a comped
  white-glove account is not frozen out from under the customer.
  Completing the trial Checkout auto-reinstates the account
  (``trial_subscription.apply_trial_checkout_completed``). Dry-run first;
  it acts on a rule the customer was told about.

* :func:`report_burner_clusters` — nightly reading of the credit-farming
  signal, now that free credits are Console-only. It names accounts and
  stops there. An earlier sweep keyed on raw-API usage, but closing the
  API to never-paid accounts did not reduce farming so much as *move* it
  to the Console, where it looks like ordinary use; a signature that
  cannot fire is worse than none, because the nightly zero reads as an
  all-clear, so that one was deleted rather than left in place.

  No single-account signal separates farming from evaluation. Burn
  velocity is the tempting one and it is wrong — an enthusiastic
  evaluator drains a grant as fast as a script does. What distinguishes
  them is repetition across accounts, so the signal is a *cluster*:
  several never-paid accounts sharing a signup origin inside a short
  window, each having drained its grant.

  This reports rather than suspends because every part of that has an
  innocent reading, and the evidence is circumstantial however many
  conditions are stacked. It was briefly wired to an automatic
  suspension and came within one signup of freezing five strangers who
  shared nothing but Console's HTTP client, back when the recorded
  origin described Console rather than the signer. Even with provenance
  captured correctly, a shared address is grounds for a person to look,
  not for software to act. Whoever reads a match can suspend through the
  admin freeze endpoint, with ``abuse_fingerprint`` as the reason.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import func, select
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


@dataclass
class ClusterReport:
    """One night's reading of the burner-cluster signal.

    Carries no verdict because the report reaches a person, who decides.
    ``flagged`` names accounts worth a look, and the counters beside it
    say whether a quiet night was quiet or blind — the run log is the
    only place anyone reads this from, and an earlier abuse signature
    was deleted for exactly that ambiguity, having become unable to fire
    while reporting the same reassuring zero every night for weeks.
    """

    #: Origin groups examined.
    scanned: int = 0
    #: Accounts in groups at or above the threshold.
    flagged: int = 0
    billing_account_ids: List[int] = field(default_factory=list)
    note: str = ""
    #: Drained never-paid accounts the report looked at.
    considered: int = 0
    #: How many of those carried no signup origin at all. Approaching
    #: ``considered`` means provenance capture has broken and the report
    #: is blind, not clear.
    without_provenance: int = 0
    #: Distinct accounts in the biggest origin group. Read against
    #: ``threshold`` this says how close the quiet run actually was.
    largest_cluster: int = 0
    #: ``burner_cluster_min_accounts`` as it was for this run.
    threshold: int = 0

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


def report_burner_clusters(session: Session) -> ClusterReport:
    """Name never-paid accounts that drained a grant from a shared origin.

    Reports; never acts. Suspending an account is available to whoever
    reads this, through the admin freeze endpoint, and stays a decision a
    person makes — every part of this signature has an innocent reading,
    and the cost of being wrong is a real customer locked out of an
    account they were about to pay for:

    * sharing a signup IP with enough other accounts
      (``burner_cluster_min_accounts``) is what an office, a VPN exit or
      a university NAT looks like;
    * those signups landing inside ``burner_cluster_window_days`` is what
      a team onboarding together looks like;
    * every account never having paid and having drained its grant is
      what a room full of people evaluating the product looks like.

    Together they are worth a look, which is what this produces. They are
    not worth an automatic suspension, and were briefly wired to one.

    Accounts with no recorded provenance are skipped rather than grouped
    under a shared ``NULL``: lumping them together would invent a cluster
    out of missing data.
    """
    result = ClusterReport(threshold=settings.burner_cluster_min_accounts)
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

    result.considered = len(users)

    # Grouped by IP alone. The stored user-agent hash is corroboration
    # for whoever reads a match, never a key: a user agent identifies a
    # browser build, so any popular one collects a crowd of strangers
    # that grows past the threshold on its own. Clustering cannot rescue
    # a coarse identifier, it multiplies it.
    clusters: dict[str, list[User]] = {}
    for user in users:
        if not user.signup_ip:
            result.without_provenance += 1
            continue
        clusters.setdefault(user.signup_ip, []).append(user)

    flagged_ba_ids: set[int] = set()
    for members in clusters.values():
        result.scanned += 1
        member_ba_ids = {u.billing_account_id for u in members if u.billing_account_id}
        result.largest_cluster = max(result.largest_cluster, len(member_ba_ids))
        if len(member_ba_ids) < settings.burner_cluster_min_accounts:
            continue
        logger.warning(
            {
                "message": "Burner cluster matched",
                "accounts": len(member_ba_ids),
                "user_agents": len(
                    {u.signup_user_agent_hash for u in members},
                ),
            },
        )
        flagged_ba_ids |= member_ba_ids

    # Accounts already suspended are left out: whoever reads this is
    # deciding what to do about an account still in use, and one that has
    # already been dealt with is noise on the next four nights' reports.
    for ba_id in sorted(flagged_ba_ids):
        ba = session.get(BillingAccount, ba_id)
        if ba is None or ba.account_status != "ACTIVE":
            continue
        result.billing_account_ids.append(ba_id)

    result.flagged = len(result.billing_account_ids)
    if result.considered and result.without_provenance == result.considered:
        # Says the quiet part out loud: every candidate was unreadable,
        # so this run proves nothing about whether farming is happening.
        result.note = (
            "No candidate carried a signup origin — this run was blind, "
            "not clear. Check that signup provenance is still recorded."
        )
    logger.warning(
        {
            "message": "Burner-cluster report finished",
            **result.to_dict(),
            "billing_account_ids": result.billing_account_ids[:50],
        },
    )
    return result
