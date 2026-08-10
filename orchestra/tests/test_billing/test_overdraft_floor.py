"""Backstop that stops a wallet accumulating debt without bound.

The pre-call spending gates are the primary control; the floor exists for
the case where spend reaches the ledger on a path that never consulted
them. It has to tolerate ordinary overshoot (a call authorised while the
balance was still positive lands afterwards) while still terminating the
unbounded case, and it has to clear itself when the debt is paid.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orchestra.db.dao.billing_account_dao import (
    OVERDRAFT_SUSPEND_FLOOR,
    BillingAccountDAO,
)
from orchestra.services.personal_workspace_service import UNIFY_ORGANIZATION_NAME
from orchestra.tests.test_billing.conftest import (
    make_billing_account,
    make_org_with_billing,
)
from orchestra.web.api.admin.schema import SuspensionReason


def _deduct(dbsession, ba, amount):
    BillingAccountDAO(dbsession).deduct_credits(
        ba.id,
        float(amount),
        category="llm",
    )
    dbsession.flush()
    dbsession.refresh(ba)
    return ba


def _add(dbsession, ba, amount):
    BillingAccountDAO(dbsession).add_credits(
        ba.id,
        float(amount),
        category="recharge",
    )
    dbsession.flush()
    dbsession.refresh(ba)
    return ba


class TestOverdraftFloor:
    def test_small_overshoot_stays_active(self, dbsession):
        """One in-flight call landing after the gate must not suspend."""
        ba = make_billing_account(dbsession, credits=Decimal("0.01"))

        _deduct(dbsession, ba, Decimal("1.00"))

        assert ba.credits < 0
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None

    def test_suspends_once_past_the_floor(self, dbsession):
        ba = make_billing_account(dbsession, credits=Decimal("0"))

        _deduct(dbsession, ba, abs(OVERDRAFT_SUSPEND_FLOOR) + Decimal("1"))

        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == SuspensionReason.OVERDRAWN.value

    def test_boundary_is_not_past_the_floor(self, dbsession):
        """Landing exactly on the floor is still within the tolerated band."""
        ba = make_billing_account(dbsession, credits=Decimal("0"))

        _deduct(dbsession, ba, abs(OVERDRAFT_SUSPEND_FLOOR))

        assert ba.credits == OVERDRAFT_SUSPEND_FLOOR
        assert ba.account_status == "ACTIVE"

    def test_accumulating_small_debits_still_trips(self, dbsession):
        """The unbounded case arrives as many small calls, not one large one."""
        ba = make_billing_account(dbsession, credits=Decimal("0"))

        for _ in range(12):
            _deduct(dbsession, ba, Decimal("0.75"))

        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == SuspensionReason.OVERDRAWN.value

    def test_existing_suspension_keeps_its_reason(self, dbsession):
        """A later deduction must not relabel why an account was frozen."""
        ba = make_billing_account(dbsession, credits=Decimal("0"))
        ba.account_status = "SUSPENDED"
        ba.suspension_reason = SuspensionReason.ABUSE_FINGERPRINT.value
        dbsession.flush()

        _deduct(dbsession, ba, abs(OVERDRAFT_SUSPEND_FLOOR) + Decimal("10"))

        assert ba.suspension_reason == SuspensionReason.ABUSE_FINGERPRINT.value

    def test_internal_accounts_are_exempt(self, dbsession):
        """Shared staging tenants and benchmarks run negative by design."""
        _, unify_ba = make_org_with_billing(
            dbsession,
            UNIFY_ORGANIZATION_NAME,
            None,
            credits=Decimal("0"),
            owner_email="founder@unify.ai",
        )

        _deduct(dbsession, unify_ba, abs(OVERDRAFT_SUSPEND_FLOOR) + Decimal("50"))

        assert unify_ba.credits < OVERDRAFT_SUSPEND_FLOOR
        assert unify_ba.account_status == "ACTIVE"


class TestOverdraftRecovery:
    def test_topping_up_lifts_the_suspension(self, dbsession):
        ba = make_billing_account(dbsession, credits=Decimal("0"))
        _deduct(dbsession, ba, abs(OVERDRAFT_SUSPEND_FLOOR) + Decimal("1"))
        assert ba.account_status == "SUSPENDED"

        _add(dbsession, ba, abs(OVERDRAFT_SUSPEND_FLOOR) + Decimal("11"))

        assert ba.credits > 0
        assert ba.account_status == "ACTIVE"
        assert ba.suspension_reason is None

    def test_partial_payment_that_stays_negative_does_not_lift(self, dbsession):
        ba = make_billing_account(dbsession, credits=Decimal("0"))
        _deduct(dbsession, ba, abs(OVERDRAFT_SUSPEND_FLOOR) + Decimal("10"))

        _add(dbsession, ba, Decimal("1"))

        assert ba.credits < 0
        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == SuspensionReason.OVERDRAWN.value

    @pytest.mark.parametrize(
        "reason",
        [
            SuspensionReason.ADMIN_FREEZE.value,
            SuspensionReason.DISPUTE.value,
            SuspensionReason.CARD_REQUIRED.value,
            SuspensionReason.ABUSE_FINGERPRINT.value,
        ],
    )
    def test_other_suspensions_survive_a_top_up(self, dbsession, reason):
        """Paying money in does not answer a dispute or a card gate."""
        ba = make_billing_account(dbsession, credits=Decimal("0"))
        ba.account_status = "SUSPENDED"
        ba.suspension_reason = reason
        dbsession.flush()

        _add(dbsession, ba, Decimal("100"))

        assert ba.account_status == "SUSPENDED"
        assert ba.suspension_reason == reason
