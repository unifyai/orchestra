import datetime
from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus


class RechargeDAO:
    """Class for accessing recharge table."""

    def __init__(self, session: Session):
        self.session = session

    def create_recharge(
        self,
        *,
        billing_account_id: int,
        quantity: int,
        amount_usd: Decimal,
        invoice_group: date,
        type_: str,
        transaction_id: str | None = None,
        status: RechargeStatus = RechargeStatus.PENDING_INVOICE,
        stripe_invoice_id: str | None = None,
    ) -> Recharge:
        """Insert a recharge row and return it."""
        recharge = Recharge(
            billing_account_id=billing_account_id,
            quantity=quantity,
            amount_usd=amount_usd,
            invoice_group=invoice_group,
            type=type_,
            transaction_id=transaction_id,
            status=status,
            stripe_invoice_id=stripe_invoice_id,
        )
        self.session.add(recharge)
        self.session.flush()
        return recharge

    def get_all_recharges(self, limit: int, offset: int) -> List[Recharge]:
        """
        Get all recharge models with limit/offset pagination.

        :param limit: limit of recharges.
        :param offset: offset of recharges.
        :return: stream of recharges.
        """
        raw_recharges = self.session.execute(
            select(Recharge).limit(limit).offset(offset),
        )

        return list(raw_recharges.scalars().fetchall())

    def filter(  # noqa: WPS211
        self,
        id: Optional[int] = None,  # noqa: WPS125
        at: Optional[datetime.datetime] = None,
        billing_account_id: Optional[int] = None,
        quantity: Optional[float] = None,
        type: Optional[str] = None,  # noqa: WPS125
    ) -> List[Recharge]:
        """
        Get specific recharge model.

        :param id: id of recharge instance.
        :param at: at of recharge instance.
        :param billing_account_id: billing_account_id of recharge instance.
        :param quantity: quantity of recharge instance.
        :param type: type of recharge instance.
        :return: stream of recharges.
        """
        query = select(Recharge)
        if id:
            query = query.where(Recharge.id == id)
        if at:
            query = query.where(Recharge.at == at)
        if billing_account_id:
            query = query.where(Recharge.billing_account_id == billing_account_id)
        if quantity:
            query = query.where(Recharge.quantity == quantity)
        if type:
            query = query.where(Recharge.type == type)

        raw_recharges = self.session.execute(query)

        return list(raw_recharges.scalars().fetchall())

    def get_recharge_by_transaction_id(self, transaction_id: str) -> Recharge:
        """
        Get a recharge by its transaction ID.

        :param transaction_id: transaction ID of the recharge.
        :return: the recharge with the given transaction ID.
        """
        raw_recharge = self.session.execute(
            select(Recharge).where(Recharge.transaction_id == transaction_id),
        )

        return raw_recharge.scalar()

    def update_recharge_status(self, recharge_id: int, status: str):
        """
        Update the status of a recharge.

        Does **not** commit — the caller is responsible for committing
        or rolling back the transaction.

        :param recharge_id: id of the recharge.
        :param status: new status of the recharge.
        """
        self.session.execute(
            update(Recharge).where(Recharge.id == recharge_id).values(status=status),
        )

    def get_recharge_by_id(self, recharge_id: int) -> Recharge:
        """
        Get a recharge by its ID.

        :param recharge_id: id of the recharge.
        :return: the recharge with the given ID.
        """
        raw_recharge = self.session.execute(
            select(Recharge).where(Recharge.id == recharge_id),
        )

        return raw_recharge.scalar()

    def get_last_paid(self, billing_account_id: int) -> Optional[Recharge]:
        """
        Get the most recent paid recharge for a billing account.

        :param billing_account_id: BillingAccount ID.
        :return: The most recent paid Recharge, or None.
        """
        return (
            self.session.execute(
                select(Recharge)
                .where(
                    Recharge.billing_account_id == billing_account_id,
                    Recharge.status == RechargeStatus.PAID,
                )
                .order_by(Recharge.at.desc())
                .limit(1),
            )
            .scalars()
            .first()
        )

    def has_pending_bills(self, billing_account_id: int) -> Tuple[bool, Decimal]:
        """
        Check if billing account has unpaid bills (PENDING_INVOICE or INVOICE_CREATED).

        Uses optimized SQL with EXISTS for performance.

        :param billing_account_id: id of the billing account.
        :return: Tuple of (has_pending_bills, total_pending_amount_usd).
        """
        result = self.session.execute(
            text(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM recharge
                        WHERE billing_account_id = :ba_id
                        AND status IN ('PENDING_INVOICE', 'INVOICE_CREATED')
                    ) as has_pending,
                    COALESCE(
                        (SELECT SUM(amount_usd) FROM recharge
                         WHERE billing_account_id = :ba_id
                         AND status IN ('PENDING_INVOICE', 'INVOICE_CREATED')),
                        0
                    ) as total_pending
            """,
            ),
            {"ba_id": billing_account_id},
        ).fetchone()
        return (result.has_pending, Decimal(str(result.total_pending)))
