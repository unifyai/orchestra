"""Tests for the credits ledger (CreditTransaction).

Covers:
- CreditTransactionDAO (insert, queries, aggregations)
- BillingAccountDAO integration (ledger rows created on add/deduct)
- Credits API endpoint (category/detail pass-through)
- Spending API endpoints (transaction history, spending breakdown)
- Levy routine ledger integration
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.credit_transaction_dao import CreditTransactionDAO
from orchestra.db.models.orchestra_models import ApiKey
from orchestra.tests.test_billing.conftest import (
    make_assistant,
    make_billing_account,
    make_contact,
    make_user_with_billing,
)


def _make_api_key(dbsession: Session, user) -> str:
    """Create an API key for *user* and return the raw key string."""
    key_value = f"test-key-{user.id}"
    dbsession.add(ApiKey(user_id=user.id, key=key_value))
    dbsession.flush()
    return key_value


# =========================================================================
# CreditTransactionDAO
# =========================================================================


class TestCreditTransactionDAO:
    """Direct tests for the credit transaction DAO."""

    def test_insert_creates_row(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        txn = dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-5.00"),
            balance_after=Decimal("95.00"),
            category="llm",
            description="GPT-4o call",
            detail={"model": "gpt-4o", "tokens": 1500},
        )

        assert txn.id is not None
        assert txn.billing_account_id == ba.id
        assert txn.amount == Decimal("-5.00")
        assert txn.balance_after == Decimal("95.00")
        assert txn.category == "llm"
        assert txn.description == "GPT-4o call"
        assert txn.detail["model"] == "gpt-4o"
        assert txn.at is not None

    def test_insert_with_all_dimensions(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        txn = dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("50.00"),
            balance_after=Decimal("150.00"),
            category="recharge",
            assistant_id=42,
            user_id="user-abc",
            organization_id=7,
            description="Test recharge",
        )

        assert txn.assistant_id == 42
        assert txn.user_id == "user-abc"
        assert txn.organization_id == 7

    def test_insert_nullable_balance_after(self, dbsession: Session):
        """Backfilled rows may have NULL balance_after."""
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        txn = dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("10.00"),
            balance_after=None,
            category="recharge",
        )

        assert txn.balance_after is None

    def test_get_transactions_pagination(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        for i in range(5):
            dao.insert(
                billing_account_id=ba.id,
                amount=Decimal("-1"),
                balance_after=Decimal(str(99 - i)),
                category="llm",
            )
        dbsession.flush()

        page1 = dao.get_transactions(ba.id, limit=2, offset=0)
        assert len(page1) == 2

        page2 = dao.get_transactions(ba.id, limit=2, offset=2)
        assert len(page2) == 2

        page3 = dao.get_transactions(ba.id, limit=2, offset=4)
        assert len(page3) == 1

    def test_get_transactions_filter_by_category(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-1"),
            balance_after=Decimal("99"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-2"),
            balance_after=Decimal("97"),
            category="media",
        )
        dbsession.flush()

        llm_only = dao.get_transactions(ba.id, category="llm")
        assert len(llm_only) == 1
        assert llm_only[0].category == "llm"

    def test_get_transactions_filter_by_assistant(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-1"),
            balance_after=Decimal("99"),
            category="llm",
            assistant_id=10,
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-2"),
            balance_after=Decimal("97"),
            category="llm",
            assistant_id=20,
        )
        dbsession.flush()

        results = dao.get_transactions(ba.id, assistant_id=10)
        assert len(results) == 1
        assert results[0].assistant_id == 10

    def test_get_transactions_filter_by_user(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-1"),
            balance_after=Decimal("99"),
            category="llm",
            user_id="alice",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-2"),
            balance_after=Decimal("97"),
            category="llm",
            user_id="bob",
        )
        dbsession.flush()

        results = dao.get_transactions(ba.id, user_id="alice")
        assert len(results) == 1
        assert results[0].user_id == "alice"

    def test_get_spending_by_category(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-3"),
            balance_after=Decimal("97"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-7"),
            balance_after=Decimal("90"),
            category="media",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("50"),
            balance_after=Decimal("140"),
            category="recharge",
        )
        dbsession.flush()

        result = dao.get_spending_by_category(
            ba.id,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        assert result["llm"] == pytest.approx(3.0)
        assert result["media"] == pytest.approx(7.0)
        assert "recharge" not in result

    def test_get_total_spend(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-10"),
            balance_after=Decimal("90"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-5"),
            balance_after=Decimal("85"),
            category="media",
        )
        dbsession.flush()

        total = dao.get_total_spend(
            ba.id,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        assert total == pytest.approx(15.0)

    def test_get_aggregated_transactions_basic(self, dbsession: Session):
        """Aggregation returns (bucket, category, total, count) tuples."""
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-3"),
            balance_after=Decimal("97"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-7"),
            balance_after=Decimal("90"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-2"),
            balance_after=Decimal("88"),
            category="media",
        )
        dbsession.flush()

        rows = dao.get_aggregated_transactions(ba.id, "day")
        assert len(rows) >= 1

        llm_rows = [r for r in rows if r[1] == "llm"]
        media_rows = [r for r in rows if r[1] == "media"]

        assert len(llm_rows) == 1
        assert llm_rows[0][2] == pytest.approx(10.0)
        assert llm_rows[0][3] == 2

        assert len(media_rows) == 1
        assert media_rows[0][2] == pytest.approx(2.0)
        assert media_rows[0][3] == 1

    def test_get_aggregated_transactions_excludes_credits(self, dbsession: Session):
        """Only debits (negative amounts) are aggregated."""
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-5"),
            balance_after=Decimal("95"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("50"),
            balance_after=Decimal("145"),
            category="recharge",
        )
        dbsession.flush()

        rows = dao.get_aggregated_transactions(ba.id, "day")
        categories = [r[1] for r in rows]
        assert "recharge" not in categories
        assert "llm" in categories

    def test_get_aggregated_transactions_filter_by_category(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-3"),
            balance_after=Decimal("97"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-2"),
            balance_after=Decimal("95"),
            category="media",
        )
        dbsession.flush()

        rows = dao.get_aggregated_transactions(ba.id, "day", category="llm")
        assert len(rows) == 1
        assert rows[0][1] == "llm"
        assert rows[0][2] == pytest.approx(3.0)

    def test_get_aggregated_transactions_filter_by_categories(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-3"),
            balance_after=Decimal("97"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-2"),
            balance_after=Decimal("95"),
            category="media",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-1"),
            balance_after=Decimal("94"),
            category="void",
        )
        dbsession.flush()

        rows = dao.get_aggregated_transactions(
            ba.id,
            "day",
            categories=["llm", "media"],
        )
        categories = {r[1] for r in rows}
        assert categories == {"llm", "media"}

    def test_get_aggregated_transactions_pagination(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        for cat in ["llm", "media", "hire", "resources"]:
            dao.insert(
                billing_account_id=ba.id,
                amount=Decimal("-1"),
                balance_after=None,
                category=cat,
            )
        dbsession.flush()

        page1 = dao.get_aggregated_transactions(ba.id, "day", limit=2, offset=0)
        page2 = dao.get_aggregated_transactions(ba.id, "day", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2

        all_categories = {r[1] for r in page1} | {r[1] for r in page2}
        assert all_categories == {"hire", "llm", "media", "resources"}

    def test_get_aggregated_transactions_filter_by_date_range(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-5"),
            balance_after=None,
            category="llm",
        )
        dbsession.flush()

        now = datetime.now(timezone.utc)
        rows_in_range = dao.get_aggregated_transactions(
            ba.id,
            "day",
            since=datetime(2020, 1, 1, tzinfo=timezone.utc),
            until=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        assert len(rows_in_range) == 1

        rows_out_of_range = dao.get_aggregated_transactions(
            ba.id,
            "day",
            since=datetime(2010, 1, 1, tzinfo=timezone.utc),
            until=datetime(2011, 1, 1, tzinfo=timezone.utc),
        )
        assert len(rows_out_of_range) == 0

    def test_get_aggregated_transactions_filter_by_assistant_and_user(
        self,
        dbsession: Session,
    ):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-3"),
            balance_after=None,
            category="llm",
            assistant_id=10,
            user_id="alice",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-7"),
            balance_after=None,
            category="llm",
            assistant_id=20,
            user_id="bob",
        )
        dbsession.flush()

        by_assistant = dao.get_aggregated_transactions(
            ba.id,
            "day",
            assistant_id=10,
        )
        assert len(by_assistant) == 1
        assert by_assistant[0][2] == pytest.approx(3.0)

        by_user = dao.get_aggregated_transactions(ba.id, "day", user_id="bob")
        assert len(by_user) == 1
        assert by_user[0][2] == pytest.approx(7.0)

    def test_get_aggregated_transactions_isolation(self, dbsession: Session):
        """Aggregated queries should be scoped to the billing account."""
        ba1 = make_billing_account(dbsession, credits=100)
        ba2 = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba1.id,
            amount=Decimal("-10"),
            balance_after=None,
            category="llm",
        )
        dao.insert(
            billing_account_id=ba2.id,
            amount=Decimal("-20"),
            balance_after=None,
            category="llm",
        )
        dbsession.flush()

        ba1_rows = dao.get_aggregated_transactions(ba1.id, "day")
        ba2_rows = dao.get_aggregated_transactions(ba2.id, "day")

        assert len(ba1_rows) == 1
        assert ba1_rows[0][2] == pytest.approx(10.0)
        assert len(ba2_rows) == 1
        assert ba2_rows[0][2] == pytest.approx(20.0)

    def test_get_balance_check(self, dbsession: Session):
        ba = make_billing_account(dbsession, credits=100)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("100"),
            balance_after=Decimal("100"),
            category="recharge",
        )
        dao.insert(
            billing_account_id=ba.id,
            amount=Decimal("-30"),
            balance_after=Decimal("70"),
            category="llm",
        )
        dbsession.flush()

        check = dao.get_balance_check(ba.id)
        assert check == Decimal("70")

    def test_isolation_between_billing_accounts(self, dbsession: Session):
        ba1 = make_billing_account(dbsession, credits=100)
        ba2 = make_billing_account(dbsession, credits=200)
        dao = CreditTransactionDAO(dbsession)

        dao.insert(
            billing_account_id=ba1.id,
            amount=Decimal("-10"),
            balance_after=Decimal("90"),
            category="llm",
        )
        dao.insert(
            billing_account_id=ba2.id,
            amount=Decimal("-20"),
            balance_after=Decimal("180"),
            category="media",
        )
        dbsession.flush()

        ba1_txns = dao.get_transactions(ba1.id)
        ba2_txns = dao.get_transactions(ba2.id)
        assert len(ba1_txns) == 1
        assert len(ba2_txns) == 1
        assert ba1_txns[0].category == "llm"
        assert ba2_txns[0].category == "media"


# =========================================================================
# BillingAccountDAO — ledger integration
# =========================================================================


class TestBillingAccountDAOLedger:
    """Verify that add_credits / deduct_credits write ledger rows."""

    def test_deduct_creates_transaction(self, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"ledger-deduct-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.deduct_credits(
            ba.id,
            10.0,
            category="llm",
            assistant_id=5,
            user_id=user.id,
            description="Test deduction",
            detail={"model": "gpt-4o"},
        )
        dbsession.flush()

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert len(txns) == 1

        txn = txns[0]
        assert float(txn.amount) == pytest.approx(-10.0)
        assert float(txn.balance_after) == pytest.approx(90.0)
        assert txn.category == "llm"
        assert txn.assistant_id == 5
        assert txn.user_id == user.id
        assert txn.description == "Test deduction"
        assert txn.detail["model"] == "gpt-4o"

    def test_add_creates_transaction(self, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"ledger-add-{uuid.uuid4().hex[:8]}",
            credits=50,
        )
        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.add_credits(
            ba.id,
            25.0,
            category="recharge",
            user_id=user.id,
            description="Test recharge",
        )
        dbsession.flush()

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert len(txns) == 1

        txn = txns[0]
        assert float(txn.amount) == pytest.approx(25.0)
        assert float(txn.balance_after) == pytest.approx(75.0)
        assert txn.category == "recharge"

    def test_multiple_deductions_accumulate(self, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"ledger-multi-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.deduct_credits(ba.id, 10.0, category="llm")
        ba_dao.deduct_credits(ba.id, 20.0, category="llm")
        ba_dao.deduct_credits(ba.id, 5.0, category="media")
        dbsession.flush()

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert len(txns) == 3

        total = txn_dao.get_total_spend(
            ba.id,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        assert total == pytest.approx(35.0)

        breakdown = txn_dao.get_spending_by_category(
            ba.id,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        assert breakdown["llm"] == pytest.approx(30.0)
        assert breakdown["media"] == pytest.approx(5.0)

    def test_negative_balance_still_records(self, dbsession: Session):
        """Deductions that push balance negative should still write ledger rows."""
        user, ba = make_user_with_billing(
            dbsession,
            f"ledger-neg-{uuid.uuid4().hex[:8]}",
            credits=5,
        )
        ba_dao = BillingAccountDAO(dbsession)

        new_balance = ba_dao.deduct_credits(ba.id, 10.0, category="llm")
        dbsession.flush()

        assert float(new_balance) == pytest.approx(-5.0)

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert len(txns) == 1
        assert float(txns[0].balance_after) == pytest.approx(-5.0)

    def test_apply_credit_grant_creates_transaction(self, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"ledger-grant-{uuid.uuid4().hex[:8]}",
            credits=0,
        )
        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.apply_credit_grant(ba.id, 10.0)
        dbsession.flush()

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert len(txns) == 1
        assert float(txns[0].amount) == pytest.approx(10.0)
        assert txns[0].category == "promo"

    def test_deduct_default_category(self, dbsession: Session):
        """When no category is given, default is 'other'."""
        user, ba = make_user_with_billing(
            dbsession,
            f"ledger-default-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.deduct_credits(ba.id, 5.0)
        dbsession.flush()

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert txns[0].category == "other"

    def test_add_default_category(self, dbsession: Session):
        """When no category is given, default is 'recharge'."""
        user, ba = make_user_with_billing(
            dbsession,
            f"ledger-add-default-{uuid.uuid4().hex[:8]}",
            credits=50,
        )
        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.add_credits(ba.id, 10.0)
        dbsession.flush()

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert txns[0].category == "recharge"


# =========================================================================
# Reconciliation: balance_check matches actual balance
# =========================================================================


class TestLedgerReconciliation:
    """Verify that SUM(amount) in the ledger equals billing_account.credits."""

    def test_reconcile_after_mixed_operations(self, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"recon-{uuid.uuid4().hex[:8]}",
            credits=0,
        )
        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.add_credits(ba.id, 100.0, category="recharge")
        ba_dao.deduct_credits(ba.id, 30.0, category="llm")
        ba_dao.deduct_credits(ba.id, 15.0, category="media")
        ba_dao.add_credits(ba.id, 50.0, category="promo")
        ba_dao.deduct_credits(ba.id, 5.0, category="hire")
        dbsession.flush()

        dbsession.refresh(ba)
        expected_balance = Decimal("100")

        txn_dao = CreditTransactionDAO(dbsession)
        ledger_sum = txn_dao.get_balance_check(ba.id)

        assert ledger_sum == expected_balance
        assert ba.credits == expected_balance


# =========================================================================
# Credits API endpoint — category/detail pass-through
# =========================================================================


class TestDeductCreditsEndpoint:
    """Test that the /credits/deduct endpoint passes category/detail through."""

    @pytest.mark.anyio
    async def test_deduct_with_category_and_detail(self, client, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"api-deduct-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={
                "amount": 5.0,
                "category": "llm",
                "assistant_id": 99,
                "description": "API test deduction",
                "detail": {"model": "gpt-4o-mini"},
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deducted"] == 5.0
        assert data["current_credits"] == pytest.approx(95.0)

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert len(txns) >= 1
        assert txns[0].category == "llm"

    @pytest.mark.anyio
    async def test_deduct_backward_compatible(self, client, dbsession: Session):
        """Old clients that only send 'amount' should still work."""
        user, ba = make_user_with_billing(
            dbsession,
            f"api-compat-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 3.0},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deducted"] == 3.0

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert txns[0].category == "llm"


# =========================================================================
# Levy routine — ledger integration
# =========================================================================


class TestLevyLedgerIntegration:
    """Verify the levy routine writes tagged ledger entries."""

    def test_levy_tags_transactions(self, dbsession: Session):
        from orchestra.db.models.orchestra_models import AssistantContactCost

        user, ba = make_user_with_billing(
            dbsession,
            f"levy-ledger-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        assistant = make_assistant(dbsession, user.id)

        existing_cost = (
            dbsession.query(AssistantContactCost)
            .filter_by(contact_type="phone", provider=None, country_code=None)
            .first()
        )
        if not existing_cost:
            dbsession.add(
                AssistantContactCost(
                    contact_type="phone",
                    monthly_cost=Decimal("2.00"),
                    one_time_cost=Decimal("1.00"),
                ),
            )
            dbsession.flush()

        contact = make_contact(
            dbsession,
            assistant.agent_id,
            contact_type="phone",
            contact_value=f"+1555{uuid.uuid4().hex[:7]}",
        )
        dbsession.flush()

        from orchestra.routines.assistant_contact_levy import _process_billing_account

        result = _process_billing_account(
            dbsession,
            ba,
            [contact],
            "2026-04",
        )

        assert result.total_amount > 0

        txn_dao = CreditTransactionDAO(dbsession)
        txns = txn_dao.get_transactions(ba.id)
        assert len(txns) >= 1

        levy_txn = [t for t in txns if t.category == "resources"]
        assert len(levy_txn) == 1
        assert levy_txn[0].detail["event"] == "contact_levy"
        assert levy_txn[0].detail["billing_month"] == "2026-04"


# =========================================================================
# Spending endpoints
# =========================================================================


class TestSpendingEndpoints:
    """Test the transaction history and spending breakdown endpoints."""

    @pytest.mark.anyio
    async def test_transaction_history_endpoint(self, client, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"api-txns-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 5.0, category="llm")
        ba_dao.deduct_credits(ba.id, 3.0, category="media")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "transactions" in data
        assert len(data["transactions"]) == 2

    @pytest.mark.anyio
    async def test_transaction_history_aggregated(self, client, dbsession: Session):
        """group_by param returns aggregated rows instead of individual ones."""
        user, ba = make_user_with_billing(
            dbsession,
            f"api-agg-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 5.0, category="llm")
        ba_dao.deduct_credits(ba.id, 3.0, category="llm")
        ba_dao.deduct_credits(ba.id, 2.0, category="media")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            params={"group_by": "day"},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "transactions" in data
        txns = data["transactions"]
        assert len(txns) >= 1

        for txn in txns:
            assert "bucket" in txn
            assert "category" in txn
            assert "total" in txn
            assert "count" in txn

        llm_rows = [t for t in txns if t["category"] == "llm"]
        assert len(llm_rows) == 1
        assert llm_rows[0]["total"] == pytest.approx(8.0)
        assert llm_rows[0]["count"] == 2

        media_rows = [t for t in txns if t["category"] == "media"]
        assert len(media_rows) == 1
        assert media_rows[0]["total"] == pytest.approx(2.0)
        assert media_rows[0]["count"] == 1

    @pytest.mark.anyio
    async def test_transaction_history_aggregated_with_granularity_prefix(
        self,
        client,
        dbsession: Session,
    ):
        """'month' is accepted as a group_by value."""
        user, ba = make_user_with_billing(
            dbsession,
            f"api-agg-month-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 1.0, category="llm")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            params={"group_by": "month"},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "transactions" in data
        assert len(data["transactions"]) >= 1

    @pytest.mark.anyio
    async def test_transaction_history_aggregated_invalid_group_by(
        self,
        client,
        dbsession: Session,
    ):
        """Invalid group_by value returns 400."""
        user, ba = make_user_with_billing(
            dbsession,
            f"api-agg-bad-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            params={"group_by": "century"},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_transaction_history_without_group_by_unchanged(
        self,
        client,
        dbsession: Session,
    ):
        """Without group_by, response is unchanged (individual rows)."""
        user, ba = make_user_with_billing(
            dbsession,
            f"api-no-agg-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 5.0, category="llm")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "transactions" in data
        txns = data["transactions"]
        assert len(txns) >= 1
        assert "id" in txns[0]
        assert "at" in txns[0]
        assert "amount" in txns[0]
        assert "bucket" not in txns[0]

    @pytest.mark.anyio
    async def test_spending_breakdown_endpoint(self, client, dbsession: Session):
        user, ba = make_user_with_billing(
            dbsession,
            f"api-spending-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = _make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 10.0, category="llm")
        ba_dao.deduct_credits(ba.id, 5.0, category="media")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/spending",
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "by_category" in data
        assert data["total"] == pytest.approx(15.0)
