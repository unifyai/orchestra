"""Async version of custom_endpoint_dao for use with AsyncSession."""

from typing import List, Tuple

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import CustomApiKey, CustomEndpoint


class AsyncCustomEndpointDAO:
    """Class for accessing custom endpoint table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_custom_endpoint(
        self,
        user_id: str,
        name: str,
        model_arg: str,
        url: str,
        key_id: int,
    ) -> None:
        self.session.add(
            CustomEndpoint(
                user_id=user_id,
                name=name,
                model_arg=model_arg,
                url=url,
                key_id=key_id,
            ),
        )

    async def filter(self, user_id: str, name: str) -> List[CustomEndpoint]:
        query = (
            select(CustomEndpoint)
            .where(CustomEndpoint.user_id == user_id)
            .where(CustomEndpoint.name == name)
        )
        raw_custom_endpoints = await self.session.execute(query)
        return list(raw_custom_endpoints.scalars().fetchall())

    async def get_user_endpoints(self, user_id: str) -> List[Tuple[str, str, str, str]]:
        query = (
            select(
                CustomEndpoint.name,
                CustomEndpoint.model_arg,
                CustomEndpoint.url,
                CustomApiKey.key,
            )
            .join(CustomApiKey, CustomEndpoint.key_id == CustomApiKey.id)
            .where(CustomEndpoint.user_id == user_id)
        )
        raw_custom_endpoints = await self.session.execute(query)
        return list(raw_custom_endpoints.fetchall())

    async def rename(self, user_id: str, name: str, new_name: str):
        query = select(CustomEndpoint)
        query = query.where(CustomEndpoint.user_id == user_id)
        query = query.where(CustomEndpoint.name == name)

        raw_custom_endpoints = await self.session.execute(query)
        custom_endpoint = raw_custom_endpoints.scalars().first()
        if custom_endpoint is not None:
            setattr(custom_endpoint, "name", new_name)

    async def delete(self, user_id: str, name: str):
        query = delete(CustomEndpoint).where(
            and_(CustomEndpoint.user_id == user_id, CustomEndpoint.name == name),
        )
        await self.session.execute(query)
