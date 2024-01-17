import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Endpoint


class EndpointDAO:
    """Class for accessing endpoint table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_endpoint(
        self,
        mdl_id: int,
        provider_id: int,
        created_at: datetime.datetime,
    ) -> None:
        """
        Add single endpoint to session.

        :param mdl_id: mdl_id of a endpoint.
        :param provider_id: provider_id of a endpoint.
        :param created_at: created_at of a endpoint.
        """
        self.session.add(
            Endpoint(
                mdl_id=mdl_id,
                provider_id=provider_id,
                created_at=created_at,
            ),
        )

    async def get_all_endpoints_raw(self, limit: int, offset: int) -> List[Endpoint]:
        """
        Get all endpoint models with limit/offset pagination.

        :param limit: limit of endpoints.
        :param offset: offset of endpoints.
        :return: stream of endpoints.
        """
        raw_endpoints = await self.session.execute(
            select(Endpoint).limit(limit).offset(offset),
        )

        return list(raw_endpoints.scalars().fetchall())

    async def filter(
        self,
        id: Optional[int] = None,  # noqa: WPS125
        mdl_id: Optional[int] = None,
        provider_id: Optional[int] = None,
        created_at: Optional[datetime.datetime] = None,
    ) -> List[Endpoint]:
        """
        Get specific endpoint model.

        :param id: id of endpoint instance.
        :param mdl_id: mdl_id of endpoint instance.
        :param provider_id: provider_id of endpoint instance.
        :param created_at: created_at of endpoint instance.
        :return: endpoint models.
        """
        query = select(Endpoint)
        if id:
            query = query.where(Endpoint.id == id)
        if mdl_id:
            query = query.where(Endpoint.mdl_id == mdl_id)
        if provider_id:
            query = query.where(Endpoint.provider_id == provider_id)
        if created_at:
            query = query.where(Endpoint.created_at == created_at)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())
