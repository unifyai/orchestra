"""Unit tests for the referral program.

Covers :mod:`orchestra.lib.referrals` and :class:`ReferralDAO`:

* attribution guards (one-per-referee, self-referral, invalid code,
  must-be-pre-payment),
* spend-gated flat reward (two-sided: flat referrer reward + flat referee
  welcome bonus), unlocking once cumulative real spend crosses the threshold,
* qualifying-spend threshold (incl. unlock on a later cycle), per-referrer
  cap, idempotency,
* refund/chargeback clawback,
* **organization support** — a referred friend who pays through an org they
  own still qualifies, and an org-scoped code credits the org.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.referral_dao import (
    STATUS_PENDING,
    STATUS_REVERSED,
    STATUS_REWARDED,
    ReferralDAO,
)
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.lib.referrals import (
    ReferralError,
    attribute_referral,
    maybe_reward_referral,
    reverse_referral_for_invoice,
)
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    make_billing_account,
    make_org,
    make_user_with_billing,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _referral_settings(monkeypatch):
    """Deterministic referral economics for the assertions below."""
    monkeypatch.setattr(settings, "referral_enabled", True, raising=False)
    monkeypatch.setattr(settings, "referral_reward_credits", 100.0, raising=False)
    monkeypatch.setattr(
        settings,
        "referral_referee_bonus_credits",
        50.0,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "referral_qualifying_spend",
        100.0,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "referral_max_rewarded_per_referrer",
        100,
        raising=False,
    )
    monkeypatch.setattr(settings, "referral_reward_expiry_days", 90, raising=False)


def _invoice(amount_usd: float, *, invoice_id: str = "in_ref_1") -> dict:
    """Synthetic paid subscription invoice (amount in cents, like Stripe)."""
    return {
        "id": invoice_id,
        "amount_paid": int(round(amount_usd * 100)),
        "billing_reason": "subscription_create",
    }


def _mark_paid(session: Session, ba_id: int, amount_usd: float = 50.0) -> None:
    """Give a billing account a real paid recharge (so it counts as 'paid')."""
    session.add(
        Recharge(
            billing_account_id=ba_id,
            type="payment",
            quantity=Decimal(str(amount_usd)),
            amount_usd=Decimal(str(amount_usd)),
            status=RechargeStatus.PAID,
        ),
    )
    session.flush()


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_attribute_creates_pending(dbsession: Session) -> None:
    referrer, _ = make_user_with_billing(dbsession, "ref_referrer")
    referee, _ = make_user_with_billing(dbsession, "ref_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)

    result = attribute_referral(dbsession, referee_user=referee, code=code.code)

    assert result.attributed is True
    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution is not None
    assert attribution.status == STATUS_PENDING
    assert attribution.referrer_user_id == referrer.id


def test_attribution_records_the_referee_s_own_origin(dbsession: Session) -> None:
    """Not the caller's, which is Console's server on every referral.

    Reading it from the request put the whole programme at one address,
    which is worth pinning because nothing consumes the column yet — the
    wrongness would only surface once someone scored against it.
    """
    referrer, _ = make_user_with_billing(dbsession, "ref_origin_referrer")
    referee, _ = make_user_with_billing(dbsession, "ref_origin_referee")
    referee.signup_ip = "198.51.100.7"
    dbsession.flush()
    code = ReferralDAO(dbsession).create_code(referrer.id)

    attribute_referral(dbsession, referee_user=referee, code=code.code)

    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.signup_ip == "198.51.100.7"


def test_attribution_without_a_known_origin_records_none(
    dbsession: Session,
) -> None:
    """A referee whose signup predates provenance has no origin to copy."""
    referrer, _ = make_user_with_billing(dbsession, "ref_noorigin_referrer")
    referee, _ = make_user_with_billing(dbsession, "ref_noorigin_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)

    attribute_referral(dbsession, referee_user=referee, code=code.code)

    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.signup_ip is None


def test_self_referral_blocked(dbsession: Session) -> None:
    user, _ = make_user_with_billing(dbsession, "ref_self")
    code = ReferralDAO(dbsession).create_code(user.id)

    with pytest.raises(ReferralError) as exc:
        attribute_referral(dbsession, referee_user=user, code=code.code)
    assert exc.value.code == "self_referral"


def test_invalid_code_raises(dbsession: Session) -> None:
    referee, _ = make_user_with_billing(dbsession, "ref_badcode")
    with pytest.raises(ReferralError) as exc:
        attribute_referral(dbsession, referee_user=referee, code="NOPE1234")
    assert exc.value.code == "invalid_code"


def test_one_attribution_per_referee(dbsession: Session) -> None:
    r1, _ = make_user_with_billing(dbsession, "ref_r1")
    r2, _ = make_user_with_billing(dbsession, "ref_r2")
    referee, _ = make_user_with_billing(dbsession, "ref_once")
    code1 = ReferralDAO(dbsession).create_code(r1.id)
    code2 = ReferralDAO(dbsession).create_code(r2.id)

    first = attribute_referral(dbsession, referee_user=referee, code=code1.code)
    assert first.attributed is True

    # A second, different code does not re-attribute.
    second = attribute_referral(dbsession, referee_user=referee, code=code2.code)
    assert second.attributed is False
    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.referrer_user_id == r1.id


def test_attribution_blocked_after_payment(dbsession: Session) -> None:
    referrer, _ = make_user_with_billing(dbsession, "ref_paid_referrer")
    referee, referee_ba = make_user_with_billing(dbsession, "ref_paid_referee")
    _mark_paid(dbsession, referee_ba.id)
    code = ReferralDAO(dbsession).create_code(referrer.id)

    result = attribute_referral(dbsession, referee_user=referee, code=code.code)
    assert result.attributed is False
    assert ReferralDAO(dbsession).get_attribution_for_referee(referee.id) is None


# ---------------------------------------------------------------------------
# Reward
# ---------------------------------------------------------------------------


def test_reward_on_qualifying_spend(dbsession: Session) -> None:
    referrer, referrer_ba = make_user_with_billing(dbsession, "rw_referrer")
    referee, referee_ba = make_user_with_billing(dbsession, "rw_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)
    attribute_referral(dbsession, referee_user=referee, code=code.code)

    dao = BillingAccountDAO(dbsession)
    before_referrer = dao.get_credits(referrer_ba.id)
    before_referee = dao.get_credits(referee_ba.id)

    # Friend subscribes and spends their first $100 of real money → unlocks.
    _mark_paid(dbsession, referee_ba.id, amount_usd=100.0)
    reward = maybe_reward_referral(dbsession, referee_ba, _invoice(100.0))

    # Flat $100 to the referrer; flat $50 welcome bonus to the referee.
    assert reward == Decimal("100")
    assert dao.get_credits(referrer_ba.id) - before_referrer == Decimal("100")
    assert dao.get_credits(referee_ba.id) - before_referee == Decimal("50")

    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.status == STATUS_REWARDED
    assert attribution.first_payment_invoice_id == "in_ref_1"


def test_reward_unlocks_on_later_cycle(dbsession: Session) -> None:
    """A lower tier qualifies only once cumulative real spend hits $100."""
    referrer, referrer_ba = make_user_with_billing(dbsession, "cyc_referrer")
    referee, referee_ba = make_user_with_billing(dbsession, "cyc_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)
    attribute_referral(dbsession, referee_user=referee, code=code.code)

    dao = BillingAccountDAO(dbsession)
    before = dao.get_credits(referrer_ba.id)

    # First $50 cycle: below the $100 threshold → no reward, stays pending.
    _mark_paid(dbsession, referee_ba.id, amount_usd=50.0)
    assert (
        maybe_reward_referral(dbsession, referee_ba, _invoice(50.0, invoice_id="in_c1"))
        is None
    )
    assert dao.get_credits(referrer_ba.id) == before
    assert (
        ReferralDAO(dbsession).get_attribution_for_referee(referee.id).status
        == STATUS_PENDING
    )

    # Second $50 cycle: cumulative $100 reached → flat reward unlocks.
    _mark_paid(dbsession, referee_ba.id, amount_usd=50.0)
    reward = maybe_reward_referral(
        dbsession,
        referee_ba,
        _invoice(50.0, invoice_id="in_c2"),
    )
    assert reward == Decimal("100")
    assert dao.get_credits(referrer_ba.id) - before == Decimal("100")
    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.status == STATUS_REWARDED
    assert attribution.first_payment_invoice_id == "in_c2"


def test_reward_below_threshold_no_reward(dbsession: Session) -> None:
    referrer, referrer_ba = make_user_with_billing(dbsession, "min_referrer")
    referee, referee_ba = make_user_with_billing(dbsession, "min_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)
    attribute_referral(dbsession, referee_user=referee, code=code.code)

    dao = BillingAccountDAO(dbsession)
    before = dao.get_credits(referrer_ba.id)

    # $20 real spend < $100 threshold → no reward, attribution stays pending.
    _mark_paid(dbsession, referee_ba.id, amount_usd=20.0)
    reward = maybe_reward_referral(dbsession, referee_ba, _invoice(20.0))
    assert reward is None
    assert dao.get_credits(referrer_ba.id) == before
    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.status == STATUS_PENDING


def test_reward_idempotent(dbsession: Session) -> None:
    referrer, referrer_ba = make_user_with_billing(dbsession, "idem_referrer")
    referee, referee_ba = make_user_with_billing(dbsession, "idem_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)
    attribute_referral(dbsession, referee_user=referee, code=code.code)

    dao = BillingAccountDAO(dbsession)
    _mark_paid(dbsession, referee_ba.id, amount_usd=100.0)
    maybe_reward_referral(dbsession, referee_ba, _invoice(100.0))
    after_first = dao.get_credits(referrer_ba.id)

    # Second delivery of a qualifying invoice must not double-grant.
    second = maybe_reward_referral(dbsession, referee_ba, _invoice(100.0))
    assert second is None
    assert dao.get_credits(referrer_ba.id) == after_first


def test_per_referrer_cap(dbsession: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "referral_max_rewarded_per_referrer",
        1,
        raising=False,
    )
    referrer, referrer_ba = make_user_with_billing(dbsession, "captot_referrer")
    code = ReferralDAO(dbsession).create_code(referrer.id)

    a, a_ba = make_user_with_billing(dbsession, "captot_a")
    b, b_ba = make_user_with_billing(dbsession, "captot_b")
    attribute_referral(dbsession, referee_user=a, code=code.code)
    attribute_referral(dbsession, referee_user=b, code=code.code)

    dao = BillingAccountDAO(dbsession)
    _mark_paid(dbsession, a_ba.id, amount_usd=100.0)
    _mark_paid(dbsession, b_ba.id, amount_usd=100.0)
    assert maybe_reward_referral(dbsession, a_ba, _invoice(100.0)) == Decimal("100")
    after_first = dao.get_credits(referrer_ba.id)

    # Second referee blocked by the cap.
    assert maybe_reward_referral(dbsession, b_ba, _invoice(100.0)) is None
    assert dao.get_credits(referrer_ba.id) == after_first
    assert (
        ReferralDAO(dbsession).get_attribution_for_referee(b.id).status
        == STATUS_PENDING
    )


# ---------------------------------------------------------------------------
# Clawback
# ---------------------------------------------------------------------------


def test_clawback_reverses(dbsession: Session) -> None:
    referrer, referrer_ba = make_user_with_billing(dbsession, "claw_referrer")
    referee, referee_ba = make_user_with_billing(dbsession, "claw_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)
    attribute_referral(dbsession, referee_user=referee, code=code.code)

    dao = BillingAccountDAO(dbsession)
    before_referrer = dao.get_credits(referrer_ba.id)
    before_referee = dao.get_credits(referee_ba.id)
    _mark_paid(dbsession, referee_ba.id, amount_usd=100.0)
    maybe_reward_referral(dbsession, referee_ba, _invoice(100.0, invoice_id="in_claw"))

    reversed_ok = reverse_referral_for_invoice(dbsession, "in_claw", reason="refund")
    assert reversed_ok is True
    assert dao.get_credits(referrer_ba.id) == before_referrer
    assert dao.get_credits(referee_ba.id) == before_referee
    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.status == STATUS_REVERSED

    # Idempotent: a second reversal is a no-op.
    assert reverse_referral_for_invoice(dbsession, "in_claw") is False


def test_clawback_no_match(dbsession: Session) -> None:
    assert reverse_referral_for_invoice(dbsession, "in_does_not_exist") is False


def test_clawback_tagged_to_originating_grant(dbsession: Session) -> None:
    """A reversal is posted as a targeted forfeit pinned to the grant lot."""
    from orchestra.db.models.orchestra_models import CreditTransaction
    from orchestra.lib.credit_grants import FORFEIT_CATEGORY, compute_grant_lots

    referrer, referrer_ba = make_user_with_billing(dbsession, "tag_referrer")
    referee, referee_ba = make_user_with_billing(dbsession, "tag_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)
    attribute_referral(dbsession, referee_user=referee, code=code.code)
    _mark_paid(dbsession, referee_ba.id, amount_usd=100.0)
    maybe_reward_referral(dbsession, referee_ba, _invoice(100.0, invoice_id="in_tag"))

    # The reward grant carries the invoice/role provenance tags.
    grant = (
        dbsession.query(CreditTransaction)
        .filter(
            CreditTransaction.billing_account_id == referrer_ba.id,
            CreditTransaction.amount > 0,
            CreditTransaction.detail["referral_role"].astext == "referrer_reward",
            CreditTransaction.detail["referral_invoice_id"].astext == "in_tag",
        )
        .one()
    )

    reverse_referral_for_invoice(dbsession, "in_tag", reason="refund")

    # The clawback debit is a targeted forfeit pinned to the grant txn.
    forfeit = (
        dbsession.query(CreditTransaction)
        .filter(
            CreditTransaction.billing_account_id == referrer_ba.id,
            CreditTransaction.category == FORFEIT_CATEGORY,
        )
        .one()
    )
    assert forfeit.detail["forfeits"][0]["grant_txn_id"] == grant.id

    # Expiry accounting matches the clawback to the original lot (remaining 0).
    lots = compute_grant_lots(dbsession, referrer_ba.id)
    grant_lot = next(lot for lot in lots if lot.txn_id == grant.id)
    assert grant_lot.remaining == Decimal("0")


# ---------------------------------------------------------------------------
# Organization support
# ---------------------------------------------------------------------------


def test_org_referee_pays_via_org(dbsession: Session) -> None:
    """A referred user who subscribes through an org they own still rewards."""
    referrer, referrer_ba = make_user_with_billing(dbsession, "orgpay_referrer")
    referee, _referee_ba = make_user_with_billing(dbsession, "orgpay_referee")
    code = ReferralDAO(dbsession).create_code(referrer.id)
    attribute_referral(dbsession, referee_user=referee, code=code.code)

    # Referee creates an org and the org's billing account makes the payment.
    org_ba = make_billing_account(dbsession)
    make_org(dbsession, referee, org_ba, name="Referee Co")

    dao = BillingAccountDAO(dbsession)
    before_referrer = dao.get_credits(referrer_ba.id)
    before_org = dao.get_credits(org_ba.id)

    _mark_paid(dbsession, org_ba.id, amount_usd=100.0)
    reward = maybe_reward_referral(dbsession, org_ba, _invoice(100.0))

    assert reward == Decimal("100")
    # Personal code → referrer's personal account earns.
    assert dao.get_credits(referrer_ba.id) - before_referrer == Decimal("100")
    # Referee bonus lands on the paying (org) account.
    assert dao.get_credits(org_ba.id) - before_org == Decimal("50")
    assert (
        ReferralDAO(dbsession).get_attribution_for_referee(referee.id).status
        == STATUS_REWARDED
    )


def test_org_scoped_code_rewards_org(dbsession: Session) -> None:
    """An org-scoped code credits the org's balance, not the referrer's."""
    referrer, referrer_ba = make_user_with_billing(dbsession, "orgcode_referrer")
    earner_org_ba = make_billing_account(dbsession)
    org = make_org(dbsession, referrer, earner_org_ba, name="Earner Org")

    code = ReferralDAO(dbsession).create_code(referrer.id, organization_id=org.id)
    assert code.referrer_organization_id == org.id

    referee, referee_ba = make_user_with_billing(dbsession, "orgcode_referee")
    result = attribute_referral(dbsession, referee_user=referee, code=code.code)
    assert result.attributed is True
    attribution = ReferralDAO(dbsession).get_attribution_for_referee(referee.id)
    assert attribution.referrer_organization_id == org.id

    dao = BillingAccountDAO(dbsession)
    before_org = dao.get_credits(earner_org_ba.id)
    before_referrer = dao.get_credits(referrer_ba.id)

    _mark_paid(dbsession, referee_ba.id, amount_usd=100.0)
    reward = maybe_reward_referral(dbsession, referee_ba, _invoice(100.0))

    assert reward == Decimal("100")
    # Reward goes to the org, not the referrer's personal account.
    assert dao.get_credits(earner_org_ba.id) - before_org == Decimal("100")
    assert dao.get_credits(referrer_ba.id) == before_referrer


def test_org_cannot_refer_existing_member(dbsession: Session) -> None:
    """An org's code can't attribute someone already in that org."""
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.role_dao import RoleDAO

    owner, _ = make_user_with_billing(dbsession, "mem_owner")
    member, _ = make_user_with_billing(dbsession, "mem_existing")
    org_ba = make_billing_account(dbsession)
    org = make_org(dbsession, owner, org_ba, name="Member Org")

    # Make `member` an actual member of the org.
    role = RoleDAO(dbsession).get_by_name("Owner", organization_id=None)
    OrganizationMemberDAO(dbsession).create(
        organization_id=org.id,
        user_id=member.id,
        role_id=role.id,
    )

    code = ReferralDAO(dbsession).create_code(owner.id, organization_id=org.id)

    with pytest.raises(ReferralError) as exc:
        attribute_referral(dbsession, referee_user=member, code=code.code)
    assert exc.value.code == "already_member"
    assert ReferralDAO(dbsession).get_attribution_for_referee(member.id) is None


def test_org_referrals_are_org_wide(dbsession: Session) -> None:
    """Org referral stats aggregate across members, not per-minter.

    A code minted by any member under the org contributes to one shared
    pool: every member sees the same org-wide signups and earnings.
    """
    owner, _ = make_user_with_billing(dbsession, "wide_owner")
    member, _ = make_user_with_billing(dbsession, "wide_member")
    org_ba = make_billing_account(dbsession)
    org = make_org(dbsession, owner, org_ba, name="Wide Org")

    dao = ReferralDAO(dbsession)
    owner_code = dao.create_code(owner.id, organization_id=org.id)
    member_code = dao.create_code(member.id, organization_id=org.id)

    f1, f1_ba = make_user_with_billing(dbsession, "wide_f1")
    f2, f2_ba = make_user_with_billing(dbsession, "wide_f2")
    attribute_referral(dbsession, referee_user=f1, code=owner_code.code)
    attribute_referral(dbsession, referee_user=f2, code=member_code.code)

    # Either member, viewing the org, sees BOTH org-scoped attributions.
    assert len(dao.list_for_referrer(owner.id, org.id)) == 2
    assert len(dao.list_for_referrer(member.id, org.id)) == 2
    # The owner's *personal* (non-org) view sees none of the org rows.
    assert dao.list_for_referrer(owner.id, None) == []

    # Earnings aggregate org-wide regardless of which member minted the code.
    _mark_paid(dbsession, f1_ba.id, amount_usd=100.0)
    _mark_paid(dbsession, f2_ba.id, amount_usd=100.0)
    maybe_reward_referral(dbsession, f1_ba, _invoice(100.0, invoice_id="in_w1"))
    maybe_reward_referral(dbsession, f2_ba, _invoice(100.0, invoice_id="in_w2"))
    assert dao.total_credits_earned(member.id, org.id) == 200.0
    assert dao.count_rewarded_for_referrer("anyone", org.id) == 2
