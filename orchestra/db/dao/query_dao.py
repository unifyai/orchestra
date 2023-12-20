import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Query


class QueryDAO:
    """Class for accessing query table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_query(
        self,
        user_id: str,
        at: datetime.datetime,
        endpoint_id: int,
        credits: float,
    ) -> None:
        """
        Add single query to session.

        :param user_id: user_id of a query.
        :param at: at of a query.
        :param endpoint_id: endpoint_id of a query.
        :param credits: credits of a query.
        """
        self.session.add(
            Query(
                user_id=user_id,
                at=at,
                endpoint_id=endpoint_id,
                credits=credits,
            ),
        )

    async def get_all_queries(self, limit: int, offset: int) -> List[Query]:
        """
        Get all query models with limit/offset pagination.

        :param limit: limit of queries.
        :param offset: offset of queries.
        :return: stream of queries.
        """
        raw_queries = await self.session.execute(
            select(Query).limit(limit).offset(offset),
        )

        return list(raw_queries.scalars().fetchall())

    async def filter(
        self,
        user_id: Optional[str] = None,
        at: Optional[datetime.datetime] = None,
        endpoint_id: Optional[int] = None,
        credits: Optional[float] = None,
    ) -> List[Query]:
        """
        Get specific query model.

        :param user_id: user_id of query instance.
        :param at: at of query instance.
        :param endpoint_id: endpoint_id of query instance.
        :param credits: credits of query instance.
        :return: query instance.
        """
        query = select(Query)
        if user_id:
            query = query.where(Query.user_id == user_id)
        if at:
            query = query.where(Query.at == at)
        if endpoint_id:
            query = query.where(Query.endpoint_id == endpoint_id)
        if credits:
            query = query.where(Query.credits == credits)

        raw_queries = await self.session.execute(query)

        return list(raw_queries.scalars().fetchall())
