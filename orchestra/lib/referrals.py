"""Referral program orchestration (attribution, reward, clawback).

Economics & anti-abuse summary
------------------------------
* **Pay on realised spend, not signup.** A reward is only ever granted once
  the referred friend has subscribed and spent their first
  ``referral_qualifying_spend`` USD of *real money* on the platform
  (cumulative paid recharges). Fake/low-value signups cost nothing.
* **Flat, two-sided reward.** Referrer credit = ``referral_reward_credits``
  (a flat USD-denominated amount, e.g. $100 → 40,000 display credits); the
  friend also gets a flat ``referral_referee_bonus_credits`` welcome bonus.
  All payouts are *free, expiring* credits (in-platform value only).
* **One reward per referee, ever.** Enforced by the unique constraint on
  ``referral_attribution.referee_user_id`` plus an idempotent, row-locked
  transition (``pending`` → ``rewarded``).
* **Self-referral blocked**, qualifying-spend threshold, per-referrer cap,
  and the referee must not have paid before being attributed.
* **Clawback** on refund / dispute reverses the granted credits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.referral_dao import (
    STATUS_PENDING,
    STATUS_REVERSED,
    STATUS_REWARDED,
    ReferralDAO,
)
from orchestra.db.models.orchestra_models import (
    BillingAccount,
    CreditTransaction,
    Organization,
    Recharge,
    RechargeStatus,
    ReferralAttribution,
    User,
)
from orchestra.lib.credit_grants import (
    FORFEIT_CATEGORY,
    GRANT_KIND_REFERRAL,
    grant_expiring_credits,
    referral_reward_expiry,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)

# Provenance tags stamped onto referral grant ledger rows (``detail``) so a
# later clawback can pin its forfeit back to the exact grant lot.
_ROLE_REFERRER_REWARD = "referrer_reward"
_ROLE_REFEREE_BONUS = "referee_bonus"


class ReferralError(Exception):
    """Attribution could not be recorded (carries a user-facing message)."""

    def __init__(self, message: str, *, code: str = "referral_error"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class AttributionResult:
    attributed: bool
    message: str
    code: Optional[str] = None


def _referee_for_billing_account(
    session: Session,
    billing_account_id: int,
) -> Optional[User]:
    """Resolve the *person* behind a paying billing account.

    Personal accounts map directly to their owning :class:`User`. For an
    organization account we attribute the payment to the org **owner**, so a
    referred user who creates an org and subscribes through it still
    qualifies the referral (once — attribution is unique per referee).
    """
    user = (
        session.query(User)
        .filter(User.billing_account_id == billing_account_id)
        .first()
    )
    if user is not None:
        return user

    org = (
        session.query(Organization)
        .filter(Organization.billing_account_id == billing_account_id)
        .first()
    )
    if org is not None and org.owner_id:
        return session.query(User).filter(User.id == org.owner_id).first()
    return None


def _reward_destination_ba_id(
    session: Session,
    attribution: ReferralAttribution,
) -> Optional[int]:
    """Billing account that should receive the referrer reward.

    Org-scoped code → the org's billing account; otherwise the referrer's
    personal billing account.
    """
    if attribution.referrer_organization_id is not None:
        org = (
            session.query(Organization)
            .filter(Organization.id == attribution.referrer_organization_id)
            .first()
        )
        if org is None or not org.billing_account_id:
            return None
        return org.billing_account_id

    referrer = (
        session.query(User).filter(User.id == attribution.referrer_user_id).first()
    )
    if referrer is None or not referrer.billing_account_id:
        return None
    return referrer.billing_account_id


def _is_member_of_org(
    session: Session,
    user_id: str,
    organization_id: Optional[int],
) -> bool:
    """True if ``user_id`` already belongs to ``organization_id``."""
    if organization_id is None:
        return False
    return (
        OrganizationMemberDAO(session).get_member(user_id, organization_id) is not None
    )


def _has_paid(session: Session, billing_account_id: int) -> bool:
    """True if the billing account has any real (non-zero) paid recharge."""
    row = (
        session.query(Recharge.id)
        .filter(
            Recharge.billing_account_id == billing_account_id,
            Recharge.status == RechargeStatus.PAID,
            Recharge.amount_usd > 0,
        )
        .first()
    )
    return row is not None


def _total_real_spend(session: Session, billing_account_id: int) -> Decimal:
    """Cumulative real money (USD) spent on a billing account.

    Sums every PAID, non-zero recharge — subscription invoices and any
    pay-as-you-go top-ups alike. Free/promotional credit grants live in the
    credit ledger, never as recharges, so they never inflate this figure.
    """
    total = (
        session.query(func.coalesce(func.sum(Recharge.amount_usd), 0))
        .filter(
            Recharge.billing_account_id == billing_account_id,
            Recharge.status == RechargeStatus.PAID,
            Recharge.amount_usd > 0,
        )
        .scalar()
    )
    return Decimal(str(total or 0))


# ──────────────────────────────────────────────────────────────────────────
# Attribution (referee signs up via a link, then claims after first login)
# ──────────────────────────────────────────────────────────────────────────


def attribute_referral(
    session: Session,
    *,
    referee_user: User,
    code: str,
    signup_ip: Optional[str] = None,
) -> AttributionResult:
    """Attribute ``referee_user`` to the owner of ``code``.

    Idempotent and safe to call repeatedly (e.g. the console retries the
    claim on every login until it succeeds, mirroring credit-grant links).
    Raises :class:`ReferralError` only for hard, surfaced failures.
    """
    if not settings.referral_enabled:
        return AttributionResult(False, "Referrals are not currently enabled.")

    dao = ReferralDAO(session)

    # Already referred? (unique per referee — first attribution wins.)
    existing = dao.get_attribution_for_referee(referee_user.id)
    if existing is not None:
        return AttributionResult(
            False,
            "This account has already been attributed to a referral.",
            code=existing.code,
        )

    referral_code = dao.get_code(code)
    if referral_code is None or referral_code.disabled_at is not None:
        raise ReferralError(
            "That referral code is invalid or no longer active.",
            code="invalid_code",
        )

    # Self-referral guard.
    if referral_code.referrer_user_id == referee_user.id:
        raise ReferralError(
            "You can't refer yourself.",
            code="self_referral",
        )

    # Org can't refer its own members. Blocking at attribution time covers
    # both the signup count and the reward — an org farming credits by
    # referring existing teammates never gets a row. (Someone who joins the
    # org *after* a legitimate external referral is unaffected.)
    if _is_member_of_org(
        session,
        referee_user.id,
        referral_code.referrer_organization_id,
    ):
        raise ReferralError(
            "You're already a member of this organization.",
            code="already_member",
        )

    # The referee must be new: no prior real payment on their personal BA.
    if referee_user.billing_account_id and _has_paid(
        session,
        referee_user.billing_account_id,
    ):
        return AttributionResult(
            False,
            "Referral codes can only be applied before your first payment.",
        )

    # Serialise concurrent claims; the unique constraint is the final guard.
    session.query(User).filter(
        User.id == referee_user.id,
    ).with_for_update().first()

    # Re-check after acquiring the lock (TOCTOU).
    if dao.get_attribution_for_referee(referee_user.id) is not None:
        return AttributionResult(
            False,
            "This account has already been attributed to a referral.",
        )

    dao.create_attribution(
        code=referral_code.code,
        referrer_user_id=referral_code.referrer_user_id,
        referrer_organization_id=referral_code.referrer_organization_id,
        referee_user_id=referee_user.id,
        referee_billing_account_id=referee_user.billing_account_id,
        signup_ip=signup_ip,
    )
    logger.info(
        {
            "message": "Referral attribution recorded",
            "code": referral_code.code,
            "referrer_user_id": referral_code.referrer_user_id,
            "referee_user_id": referee_user.id,
        },
    )
    return AttributionResult(
        True,
        "Referral applied! You'll earn a bonus once your first payment clears.",
        code=referral_code.code,
    )


# ──────────────────────────────────────────────────────────────────────────
# Reward (friend subscribes and reaches the qualifying real spend)
# ──────────────────────────────────────────────────────────────────────────


def maybe_reward_referral(
    session: Session,
    billing_account: BillingAccount,
    invoice: dict,
) -> Optional[Decimal]:
    """Reward the referrer when a referred friend qualifies.

    Call from the ``invoice.paid`` webhook path for any paid subscription
    invoice (create or renewal cycle): the reward unlocks once the friend has
    subscribed and their cumulative real-money spend crosses
    ``referral_qualifying_spend``, which can land on a later cycle for lower
    tiers. No-op (returns ``None``) unless every guard passes. The reward is
    in free, expiring credits and is idempotent on the attribution status.
    """
    if not settings.referral_enabled:
        return None

    referee = _referee_for_billing_account(session, billing_account.id)
    if referee is None:
        return None

    dao = ReferralDAO(session)
    attribution = dao.get_attribution_for_referee(referee.id)
    if attribution is None or attribution.status != STATUS_PENDING:
        return None

    # Lock + re-check to make the transition idempotent under concurrent
    # webhook delivery.
    locked = dao.lock_attribution(attribution.id)
    if locked is None or locked.status != STATUS_PENDING:
        return None
    attribution = locked

    # Spend gate: the friend must have subscribed (this call runs off a paid
    # subscription invoice) AND spent at least ``referral_qualifying_spend``
    # of real money cumulatively. Stays pending until the threshold is met,
    # so a lower tier qualifies on a later renewal cycle.
    real_spend = _total_real_spend(session, billing_account.id)
    if real_spend < Decimal(str(settings.referral_qualifying_spend)):
        logger.info(
            {
                "message": "Referral real spend below qualifying threshold",
                "referee_user_id": referee.id,
                "real_spend_usd": float(real_spend),
                "threshold": settings.referral_qualifying_spend,
            },
        )
        return None

    # Per-referrer reward cap. Org-scoped codes share one cap across the whole
    # org; personal codes are capped per user.
    cap = settings.referral_max_rewarded_per_referrer
    if (
        cap
        and dao.count_rewarded_for_referrer(
            attribution.referrer_user_id,
            attribution.referrer_organization_id,
        )
        >= cap
    ):
        logger.warning(
            {
                "message": "Referrer reward cap reached; skipping referral reward",
                "referrer_user_id": attribution.referrer_user_id,
                "cap": cap,
            },
        )
        return None

    # Resolve where the referrer reward lands: the org's billing account for
    # an org-scoped code, otherwise the referrer's personal account.
    reward_ba_id = _reward_destination_ba_id(session, attribution)
    if reward_ba_id is None:
        return None

    # Flat referrer reward + flat referee welcome bonus (both USD value).
    reward = Decimal(str(settings.referral_reward_credits))
    referee_bonus = Decimal(str(settings.referral_referee_bonus_credits))

    expires_at = referral_reward_expiry()
    invoice_id = invoice.get("id")

    if reward > 0:
        grant_expiring_credits(
            session,
            reward_ba_id,
            reward,
            grant_kind=GRANT_KIND_REFERRAL,
            expires_at=expires_at,
            description=f"Referral reward (friend subscribed, invoice {invoice_id})",
            detail_extra={
                "referral_invoice_id": invoice_id,
                "referral_role": _ROLE_REFERRER_REWARD,
            },
        )
    if referee_bonus > 0:
        grant_expiring_credits(
            session,
            billing_account.id,
            referee_bonus,
            grant_kind=GRANT_KIND_REFERRAL,
            expires_at=expires_at,
            description="Referral welcome bonus (qualifying spend reached)",
            user_id=referee.id,
            detail_extra={
                "referral_invoice_id": invoice_id,
                "referral_role": _ROLE_REFEREE_BONUS,
            },
        )

    attribution.status = STATUS_REWARDED
    attribution.rewarded_at = ReferralDAO.now()
    attribution.first_payment_invoice_id = invoice_id
    attribution.referee_billing_account_id = billing_account.id
    attribution.referrer_billing_account_id = reward_ba_id
    attribution.reward_amount = reward
    attribution.referee_bonus_amount = referee_bonus
    session.flush()

    logger.info(
        {
            "message": "Referral reward granted",
            "code": attribution.code,
            "referrer_user_id": attribution.referrer_user_id,
            "referee_user_id": referee.id,
            "reward_credits": float(reward),
            "referee_bonus_credits": float(referee_bonus),
            "invoice_id": invoice_id,
        },
    )
    return reward


# ──────────────────────────────────────────────────────────────────────────
# Clawback (refund / chargeback on the qualifying invoice)
# ──────────────────────────────────────────────────────────────────────────


def _find_referral_grant_txn_id(
    session: Session,
    *,
    billing_account_id: int,
    invoice_id: str,
    role: str,
) -> Optional[int]:
    """Locate the positive referral grant ledger row for a BA + invoice + role.

    Returns the :class:`CreditTransaction` id of the grant so a clawback can
    pin its forfeit to that exact lot. ``None`` for legacy grants written
    before the invoice/role provenance tags existed.
    """
    row = (
        session.query(CreditTransaction.id)
        .filter(
            CreditTransaction.billing_account_id == billing_account_id,
            CreditTransaction.amount > 0,
            CreditTransaction.detail["grant_kind"].astext == GRANT_KIND_REFERRAL,
            CreditTransaction.detail["referral_invoice_id"].astext == invoice_id,
            CreditTransaction.detail["referral_role"].astext == role,
        )
        .order_by(CreditTransaction.id.desc())
        .first()
    )
    return int(row[0]) if row is not None else None


def _reverse_referral_grant(
    session: Session,
    ba_dao: BillingAccountDAO,
    *,
    billing_account_id: int,
    amount: Decimal,
    invoice_id: str,
    role: str,
    description: str,
    detail: dict,
) -> None:
    """Debit a previously granted referral credit for a clawback.

    When the originating grant lot can be located, the debit is posted as a
    *targeted forfeit* (``category='forfeit'`` with ``detail.forfeits`` pinning
    the grant txn id) so :func:`compute_grant_lots` matches the clawback to the
    right expiring lot — keeping expiry accounting accurate and idempotent.
    Legacy grants (no provenance tag) fall back to a plain ``refund`` debit.
    """
    if amount <= 0:
        return

    grant_txn_id = _find_referral_grant_txn_id(
        session,
        billing_account_id=billing_account_id,
        invoice_id=invoice_id,
        role=role,
    )

    if grant_txn_id is not None:
        ba_dao.deduct_credits(
            billing_account_id,
            float(amount),
            category=FORFEIT_CATEGORY,
            description=description,
            detail={
                **detail,
                "forfeits": [{"grant_txn_id": grant_txn_id, "amount": str(amount)}],
            },
        )
    else:
        ba_dao.deduct_credits(
            billing_account_id,
            float(amount),
            category="refund",
            description=description,
            detail=detail,
        )


def reverse_referral_for_invoice(
    session: Session,
    invoice_id: Optional[str],
    *,
    reason: str = "refund",
) -> bool:
    """Reverse a referral reward whose qualifying invoice was refunded/disputed.

    Debits the previously granted credits from both the referrer and the
    referee and flips the attribution to ``reversed``. Best-effort and
    idempotent: a no-op if there is no rewarded attribution for ``invoice_id``.
    """
    if not invoice_id:
        return False

    dao = ReferralDAO(session)
    attribution = dao.get_rewarded_by_invoice(invoice_id)
    if attribution is None:
        return False

    locked = dao.lock_attribution(attribution.id)
    if locked is None or locked.status != STATUS_REWARDED:
        return False
    attribution = locked

    ba_dao = BillingAccountDAO(session)

    detail = {
        "event": "referral_reversal",
        "reason": reason,
        "invoice_id": invoice_id,
        "attribution_id": attribution.id,
    }

    # Reward was granted to the recorded destination BA (org or personal);
    # fall back to recomputing it for older rows.
    reward_ba_id = attribution.referrer_billing_account_id or _reward_destination_ba_id(
        session,
        attribution,
    )
    reward = Decimal(str(attribution.reward_amount or 0))
    if reward_ba_id and reward > 0 and invoice_id:
        _reverse_referral_grant(
            session,
            ba_dao,
            billing_account_id=reward_ba_id,
            amount=reward,
            invoice_id=invoice_id,
            role=_ROLE_REFERRER_REWARD,
            description="Referral reward reversed (friend's payment refunded)",
            detail=detail,
        )

    bonus = Decimal(str(attribution.referee_bonus_amount or 0))
    if attribution.referee_billing_account_id and bonus > 0 and invoice_id:
        _reverse_referral_grant(
            session,
            ba_dao,
            billing_account_id=attribution.referee_billing_account_id,
            amount=bonus,
            invoice_id=invoice_id,
            role=_ROLE_REFEREE_BONUS,
            description="Referral welcome bonus reversed (payment refunded)",
            detail=detail,
        )

    attribution.status = STATUS_REVERSED
    attribution.reversed_at = ReferralDAO.now()
    session.flush()

    logger.info(
        {
            "message": "Referral reward reversed",
            "code": attribution.code,
            "referrer_user_id": attribution.referrer_user_id,
            "referee_user_id": attribution.referee_user_id,
            "invoice_id": invoice_id,
            "reason": reason,
        },
    )
    return True
