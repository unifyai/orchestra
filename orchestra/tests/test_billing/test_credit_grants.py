"""Unit tests for the expiring credit-grant ledger math.

Covers :mod:`orchestra.lib.credit_grants` (the pure forfeit/allocation
logic — no scheduled routines, which live in ``test_billing_routines``):

* trial grant forfeited (balance zeroed) once expired,
* partial consumption then forfeit of the unconsumed remainder,
* non-expiring credits never forfeited,
* soonest-expiry-first consumption ordering,
* idempotency of the forfeit (re-run is a no-op),
* plan-grant cycle-reset forfeit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.lib.credit_grants import (
    GRANT_KIND_PLAN,
    GRANT_KIND_TRIAL,
    compute_grant_lots,
    forfeit_expired_grants,
    forfeit_plan_grant_remainder,
    grant_expiring_credits,
)
from orchestra.tests.test_billing.conftest import make_user_with_billing


def _past(days: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _future(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_expired_trial_grant_is_forfeited(dbsession: Session) -> None:
    _user, ba = make_user_with_billing(dbsession, "grant_trial")
    dao = BillingAccountDAO(dbsession)

    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_TRIAL,
        expires_at=_past(),
    )
    dbsession.flush()
    assert dao.get_credits(ba.id) == Decimal("100")

    forfeited = forfeit_expired_grants(dbsession, ba)
    assert forfeited == Decimal("100")
    assert dao.get_credits(ba.id) == Decimal("0")


def test_partial_consumption_then_forfeit_remainder(dbsession: Session) -> None:
    _user, ba = make_user_with_billing(dbsession, "grant_partial")
    dao = BillingAccountDAO(dbsession)

    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_TRIAL,
        expires_at=_past(),
    )
    dao.deduct_credits(ba.id, 30, category="llm")
    dbsession.flush()
    assert dao.get_credits(ba.id) == Decimal("70")

    forfeited = forfeit_expired_grants(dbsession, ba)
    assert forfeited == Decimal("70")
    assert dao.get_credits(ba.id) == Decimal("0")


def test_non_expiring_credits_never_forfeited(dbsession: Session) -> None:
    _user, ba = make_user_with_billing(dbsession, "grant_mixed")
    dao = BillingAccountDAO(dbsession)

    # Non-expiring top-up (no grant_kind) + an expired trial grant.
    dao.add_credits(ba.id, 50, category="recharge")
    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_TRIAL,
        expires_at=_past(),
    )
    dbsession.flush()
    assert dao.get_credits(ba.id) == Decimal("150")

    forfeited = forfeit_expired_grants(dbsession, ba)
    assert forfeited == Decimal("100")
    assert dao.get_credits(ba.id) == Decimal("50")


def test_consumption_attributed_soonest_expiry_first(dbsession: Session) -> None:
    _user, ba = make_user_with_billing(dbsession, "grant_order")
    dao = BillingAccountDAO(dbsession)

    # Soonest-expiring (already expired) trial + a later-expiring plan grant.
    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_TRIAL,
        expires_at=_past(),
    )
    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_PLAN,
        expires_at=_future(),
    )
    # Spend 40 — should be drawn from the soonest-expiring trial lot first.
    dao.deduct_credits(ba.id, 40, category="llm")
    dbsession.flush()

    forfeited = forfeit_expired_grants(dbsession, ba)
    # Only the expired trial remainder (100 - 40 = 60) forfeits; the
    # not-yet-expired plan grant (100) is untouched.
    assert forfeited == Decimal("60")
    assert dao.get_credits(ba.id) == Decimal("100")


def test_forfeit_is_idempotent(dbsession: Session) -> None:
    _user, ba = make_user_with_billing(dbsession, "grant_idem")
    dao = BillingAccountDAO(dbsession)

    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_TRIAL,
        expires_at=_past(),
    )
    dbsession.flush()

    assert forfeit_expired_grants(dbsession, ba) == Decimal("100")
    assert dao.get_credits(ba.id) == Decimal("0")
    # Re-run: nothing left to forfeit.
    assert forfeit_expired_grants(dbsession, ba) == Decimal("0")
    assert dao.get_credits(ba.id) == Decimal("0")

    lots = compute_grant_lots(dbsession, ba.id)
    assert all(lot.remaining == Decimal("0") for lot in lots)


def test_plan_grant_remainder_forfeit_forced(dbsession: Session) -> None:
    _user, ba = make_user_with_billing(dbsession, "grant_plan_reset")
    dao = BillingAccountDAO(dbsession)

    # Plan grant not yet at its nominal expiry, but the cycle boundary
    # forces the reset.
    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_PLAN,
        expires_at=_future(),
    )
    dao.deduct_credits(ba.id, 25, category="llm")
    dbsession.flush()

    forfeited = forfeit_plan_grant_remainder(dbsession, ba)
    assert forfeited == Decimal("75")
    assert dao.get_credits(ba.id) == Decimal("0")


def test_grant_ledger_category_splits_paid_vs_free(dbsession: Session) -> None:
    """Subscription (``plan``) grants record as the *paid* category
    ``subscription_recharge``; ``trial``/goodwill grants stay ``grant``.

    The expiry tag (``detail.grant_kind``) is identical either way — only the
    ledger ``category`` differs, so paid-vs-free reporting is clean."""
    from orchestra.db.models.orchestra_models import CreditTransaction

    _user, ba = make_user_with_billing(dbsession, "grant_category_split")

    grant_expiring_credits(
        dbsession,
        ba.id,
        100,
        grant_kind=GRANT_KIND_PLAN,
        expires_at=_future(),
    )
    grant_expiring_credits(
        dbsession,
        ba.id,
        25,
        grant_kind=GRANT_KIND_TRIAL,
        expires_at=_future(),
    )
    dbsession.flush()

    rows = {
        (txn.detail or {}).get("grant_kind"): txn
        for txn in dbsession.query(CreditTransaction)
        .filter(CreditTransaction.billing_account_id == ba.id)
        .all()
    }
    assert rows[GRANT_KIND_PLAN].category == "subscription_recharge"
    assert rows[GRANT_KIND_TRIAL].category == "grant"
