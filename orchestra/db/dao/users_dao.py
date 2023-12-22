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

    async def get_all_users(self, limit: int, offset: int) -> List[Users]:
        """
        Get all users models with limit/offset pagination.

        :param limit: limit of users.
        :param offset: offset of users.
        :return: stream of users.
        """
        raw_users = await self.session.execute(
            select(Users).limit(limit).offset(offset),
        )

        return list(raw_users.scalars().fetchall())

    async def filter(
        self,
        id: Optional[str] = None,  # noqa: WPS125
        credits: Optional[float] = None,
    ) -> List[Users]:
        """
        Get specific users model.

        :param id: id of users instance.
        :param credits: credits of users instance.
        :return: stream of users.
        """
        query = select(Users)
        if id:
            query = query.where(Users.id == id)
        if credits:
            query = query.where(Users.credits == credits)

        raw_users = await self.session.execute(query)

        return list(raw_users.scalars().fetchall())
