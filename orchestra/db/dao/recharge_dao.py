import datetime
from datetime import date
from decimal import Decimal
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus


class RechargeDAO:
    """Class for accessing recharge table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_recharge(
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

        :param recharge_id: id of the recharge.
        :param status: new status of the recharge.
        """
        self.session.execute(
            update(Recharge).where(Recharge.id == recharge_id).values(status=status),
        )
        self.session.commit()

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
