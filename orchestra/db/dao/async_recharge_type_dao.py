"""Async version of recharge_type_dao for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import RechargeType


class AsyncRechargeTypeDAO:
    """Class for accessing recharge_type table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_recharge_type(
        self,
        type: str,  # noqa: WPS125
    ) -> None:
        """
        Add single recharge_type to session.

        :param type: type of a recharge_type.
        """
        self.session.add(
            RechargeType(
                type=type,
            ),
        )

    async def get_all_recharge_types(
        self,
        limit: int,
        offset: int,
    ) -> List[RechargeType]:
        """
        Get all recharge_type models with limit/offset pagination.

        :param limit: limit of recharge_types.
        :param offset: offset of recharge_types.
        :return: stream of recharge_types.
        """
        raw_recharge_types = await self.session.execute(
            select(RechargeType).limit(limit).offset(offset),
        )

        return list(raw_recharge_types.scalars().fetchall())

    async def filter(
        self,
        type: Optional[str] = None,  # noqa: WPS125
    ) -> List[RechargeType]:
        """
        Get specific recharge_type model.

        :param type: type of recharge_type instance.
        :return: stream of recharge_types.
        """
        query = select(RechargeType)
        if type:
            query = query.where(RechargeType.type == type)

        raw_recharge_types = await self.session.execute(query)

        return list(raw_recharge_types.scalars().fetchall())
