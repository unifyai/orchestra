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
        model_id: int,
        provider_id: int,
        created_at: str,
    ) -> None:
        """
        Add single endpoint to session.

        :param model_id: model_id of a endpoint.
        :param provider_id: provider_id of a endpoint.
        :param created_at: created_at of a endpoint.
        """
        self.session.add(
            Endpoint(
                model_id=model_id,
                provider_id=provider_id,
                created_at=created_at,
            ),
        )

    async def get_all_endpoints(self, limit: int, offset: int) -> List[Endpoint]:
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
        model_id: Optional[int] = None,
        provider_id: Optional[int] = None,
    ) -> List[Endpoint]:
        """
        Get specific endpoint model.

        :param model_id: model_id of endpoint instance.
        :param provider_id: provider_id of endpoint instance.
        :return: endpoint models.
        """
        query = select(Endpoint)
        if model_id:
            query = query.where(Endpoint.model_id == model_id)
        if provider_id:
            query = query.where(Endpoint.provider_id == provider_id)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())
