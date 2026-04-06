"""Data Access Object for CreditTransaction (the credits ledger)."""

from __future__ import annotations

import decimal
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import CreditTransaction

logger = logging.getLogger(__name__)


class CreditTransactionDAO:
    """DAO for querying the credit_transaction ledger."""

    def __init__(self, session: Session):
        self.session = session

    def insert(
        self,
        *,
        billing_account_id: int,
        amount: decimal.Decimal,
        balance_after: decimal.Decimal | None,
        category: str,
        assistant_id: int | None = None,
        user_id: str | None = None,
        organization_id: int | None = None,
        description: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> CreditTransaction:
        """Insert a new ledger row. Called by BillingAccountDAO, not directly."""
        txn = CreditTransaction(
            billing_account_id=billing_account_id,
            amount=amount,
            balance_after=balance_after,
            category=category,
            assistant_id=assistant_id,
            user_id=user_id,
            organization_id=organization_id,
            description=description,
            detail=detail,
        )
        self.session.add(txn)
        self.session.flush()
        return txn

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_transactions(
        self,
        billing_account_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
        category: str | None = None,
        assistant_id: int | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[CreditTransaction]:
        """Paginated transaction history for a billing account."""
        q = self.session.query(CreditTransaction).filter(
            CreditTransaction.billing_account_id == billing_account_id,
        )
        if category:
            q = q.filter(CreditTransaction.category == category)
        if assistant_id is not None:
            q = q.filter(CreditTransaction.assistant_id == assistant_id)
        if user_id is not None:
            q = q.filter(CreditTransaction.user_id == user_id)
        if since is not None:
            q = q.filter(CreditTransaction.at >= since)
        if until is not None:
            q = q.filter(CreditTransaction.at < until)

        return q.order_by(CreditTransaction.at.desc()).limit(limit).offset(offset).all()

    def get_spending_by_category(
        self,
        billing_account_id: int,
        month_start: datetime,
        month_end: datetime,
        *,
        user_id: str | None = None,
        assistant_id: int | None = None,
    ) -> dict[str, float]:
        """Return ``{category: total_spend}`` for debits in the given window."""
        q = (
            self.session.query(
                CreditTransaction.category,
                func.sum(-CreditTransaction.amount).label("total"),
            )
            .filter(
                CreditTransaction.billing_account_id == billing_account_id,
                CreditTransaction.amount < 0,
                CreditTransaction.at >= month_start,
                CreditTransaction.at < month_end,
            )
            .group_by(CreditTransaction.category)
        )
        if user_id is not None:
            q = q.filter(CreditTransaction.user_id == user_id)
        if assistant_id is not None:
            q = q.filter(CreditTransaction.assistant_id == assistant_id)

        return {cat: float(total) for cat, total in q.all()}

    def get_total_spend(
        self,
        billing_account_id: int,
        month_start: datetime,
        month_end: datetime,
        *,
        assistant_id: int | None = None,
        user_id: str | None = None,
        organization_id: int | None = None,
    ) -> float:
        """Total credits spent (debits only) in the given window."""
        q = self.session.query(
            func.coalesce(func.sum(-CreditTransaction.amount), 0),
        ).filter(
            CreditTransaction.billing_account_id == billing_account_id,
            CreditTransaction.amount < 0,
            CreditTransaction.at >= month_start,
            CreditTransaction.at < month_end,
        )
        if assistant_id is not None:
            q = q.filter(CreditTransaction.assistant_id == assistant_id)
        if user_id is not None:
            q = q.filter(CreditTransaction.user_id == user_id)
        if organization_id is not None:
            q = q.filter(CreditTransaction.organization_id == organization_id)
        return float(q.scalar())

    def get_balance_check(self, billing_account_id: int) -> Optional[decimal.Decimal]:
        """Return ``SUM(amount)`` for all transactions — should equal ``credits``."""
        result = (
            self.session.query(
                func.sum(CreditTransaction.amount),
            )
            .filter(
                CreditTransaction.billing_account_id == billing_account_id,
            )
            .scalar()
        )
        return decimal.Decimal(str(result)) if result is not None else None
