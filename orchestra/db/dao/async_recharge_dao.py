"""Async version of recharge_dao for use with AsyncSession."""

import datetime
from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus


class AsyncRechargeDAO:
    """Class for accessing recharge table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_recharge(
        self,
        *,
        user_id: str,
        quantity: int,
        amount_usd: Decimal,
        invoice_group: date,
        type_: str,
        transaction_id: str | None = None,
        status: RechargeStatus = RechargeStatus.PENDING_INVOICE,
    ) -> Recharge:
        """Insert a recharge row and return it."""
        recharge = Recharge(
            user_id=user_id,
            quantity=quantity,
            amount_usd=amount_usd,
            invoice_group=invoice_group,
            type=type_,
            transaction_id=transaction_id,
            status=status,
        )
        self.session.add(recharge)
        await self.session.flush()
        return recharge

    async def get_all_recharges(self, limit: int, offset: int) -> List[Recharge]:
        """
        Get all recharge models with limit/offset pagination.

        :param limit: limit of recharges.
        :param offset: offset of recharges.
        :return: stream of recharges.
        """
        raw_recharges = await self.session.execute(
            select(Recharge).limit(limit).offset(offset),
        )

        return list(raw_recharges.scalars().fetchall())

    async def filter(  # noqa: WPS211
        self,
        id: Optional[int] = None,  # noqa: WPS125
        at: Optional[datetime.datetime] = None,
        user_id: Optional[str] = None,
        quantity: Optional[float] = None,
        type: Optional[str] = None,  # noqa: WPS125
    ) -> List[Recharge]:
        """
        Get specific recharge model.

        :param id: id of recharge instance.
        :param at: at of recharge instance.
        :param user_id: user_id of recharge instance.
        :param quantity: quantity of recharge instance.
        :param type: type of recharge instance.
        :return: stream of recharges.
        """
        query = select(Recharge)
        if id:
            query = query.where(Recharge.id == id)
        if at:
            query = query.where(Recharge.at == at)
        if user_id:
            query = query.where(Recharge.user_id == user_id)
        if quantity:
            query = query.where(Recharge.quantity == quantity)
        if type:
            query = query.where(Recharge.type == type)

        raw_recharges = await self.session.execute(query)

        return list(raw_recharges.scalars().fetchall())

    async def get_recharge_by_transaction_id(self, transaction_id: str) -> Recharge:
        """
        Get a recharge by its transaction ID.

        :param transaction_id: transaction ID of the recharge.
        :return: the recharge with the given transaction ID.
        """
        raw_recharge = await self.session.execute(
            select(Recharge).where(Recharge.transaction_id == transaction_id),
        )

        return raw_recharge.scalar()

    async def update_recharge_status(self, recharge_id: int, status: str):
        """
        Update the status of a recharge.

        :param recharge_id: id of the recharge.
        :param status: new status of the recharge.
        """
        await self.session.execute(
            update(Recharge).where(Recharge.id == recharge_id).values(status=status),
        )
        await self.session.commit()

    async def get_recharge_by_id(self, recharge_id: int) -> Recharge:
        """
        Get a recharge by its ID.

        :param recharge_id: id of the recharge.
        :return: the recharge with the given ID.
        """
        raw_recharge = await self.session.execute(
            select(Recharge).where(Recharge.id == recharge_id),
        )

        return raw_recharge.scalar()

    async def has_pending_bills(self, user_id: str) -> Tuple[bool, Decimal]:
        """
        Check if user has unpaid bills (PENDING_INVOICE or INVOICE_CREATED).

        Uses optimized SQL with EXISTS for performance.

        :param user_id: id of the user.
        :return: Tuple of (has_pending_bills, total_pending_amount_usd).
        """
        result = await self.session.execute(
            text(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM recharge
                        WHERE user_id = :uid
                        AND status IN ('PENDING_INVOICE', 'INVOICE_CREATED')
                    ) as has_pending,
                    COALESCE(
                        (SELECT SUM(amount_usd) FROM recharge
                         WHERE user_id = :uid
                         AND status IN ('PENDING_INVOICE', 'INVOICE_CREATED')),
                        0
                    ) as total_pending
            """,
            ),
            {"uid": user_id},
        ).fetchone()
        return (result.has_pending, Decimal(str(result.total_pending)))
