import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Recharge


class RechargeDAO:
    """Class for accessing recharge table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_recharge(
        self,
        at: datetime.datetime,
        user_id: str,
        quantity: float,
        type: str,  # noqa: WPS125
    ) -> None:
        """
        Add single recharge to session.

        :param at: at of a recharge.
        :param user_id: user_id of a recharge.
        :param quantity: quantity of a recharge.
        :param type: type of a recharge.
        """
        self.session.add(
            Recharge(
                at=at,
                user_id=user_id,
                quantity=quantity,
                type=type,
            ),
        )

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
