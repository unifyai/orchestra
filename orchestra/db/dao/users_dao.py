import decimal
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Users


class UsersDAO:
    """Class for accessing users table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_users(
        self,
        id: str,  # noqa: WPS125
        credits: float,
    ) -> None:
        """
        Add single users to session.

        :param id: id of a users.
        :param credits: credits of a users.
        """
        self.session.add(
            Users(
                id=id,
                credits=credits,
            ),
        )

    async def get_all_users(self) -> List[Users]:
        """
        Get all users models with limit/offset pagination.

        :return: stream of users.
        """
        raw_users = await self.session.execute(
            select(Users),
        )

        return list(raw_users.scalars().fetchall())

    async def filter(
        self,
        id: Optional[str],  # noqa: WPS125
    ) -> List[Users]:
        """
        Get specific users model.

        :param id: id of users instance.
        :return: stream of users.
        """
        query = select(Users)
        query = query.where(Users.id == id)

        raw_users = await self.session.execute(query)

        return list(raw_users.scalars().fetchall())

    async def recharge_credit(
        self,
        user_id: str,
        quantity: float,
    ) -> None:
        """
        Recharge credit of a users.

        :param user_id: id of a user.
        :param quantity: positive number of credits to recharge.
        """
        query = select(Users)
        query = query.where(Users.id == user_id)

        raw_users = await self.session.execute(query)
        user = raw_users.scalars().first()
        if user is not None:
            setattr(  # noqa: B010
                user,
                "credits",
                user.credits + decimal.Decimal(quantity),
            )
