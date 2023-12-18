from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import User


class UserDAO:
    """Class for accessing user table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_user(
        self,
        id: str,  # noqa: WPS125
        credits: float,
    ) -> None:
        """
        Add single user to session.

        :param id: id of a user.
        :param credits: credits of a user.
        """
        self.session.add(
            User(
                id=id,
                credits=credits,
            ),
        )

    async def get_all_users(self, limit: int, offset: int) -> List[User]:
        """
        Get all user models with limit/offset pagination.

        :param limit: limit of users.
        :param offset: offset of users.
        :return: stream of users.
        """
        raw_users = await self.session.execute(
            select(User).limit(limit).offset(offset),
        )

        return list(raw_users.scalars().fetchall())

    async def filter(
        self,
        id: Optional[str] = None,  # noqa: WPS125
        credits: Optional[float] = None,
    ) -> List[User]:
        """
        Get specific user model.

        :param id: id of user instance.
        :param credits: credits of user instance.
        :return: stream of users.
        """
        query = select(User)
        if id:
            query = query.where(User.id == id)
        if credits:
            query = query.where(User.credits == credits)

        raw_users = await self.session.execute(query)

        return list(raw_users.scalars().fetchall())
