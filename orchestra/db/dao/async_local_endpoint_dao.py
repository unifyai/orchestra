"""Async version of local_endpoint_dao for use with AsyncSession."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import LocalEndpoint


class AsyncLocalEndpointDAO:
    """Class for accessing local endpoint table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create_local_endpoint(
        self,
        user_id: str,
        name: str,
    ) -> int:

        # try and find the endpoint
        stmt = select(LocalEndpoint.id).where(
            LocalEndpoint.user_id == user_id,
            LocalEndpoint.name == name,
        )
        endpoint = list(await self.session.execute(stmt).fetchall())
        if endpoint:
            return endpoint[0].id

        # add if not
        try:
            stmt = insert(LocalEndpoint).values(user_id=user_id, name=name)
            stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "name"])
            result = await self.session.execute(stmt)
            await self.session.commit()

            existing_stmt = select(LocalEndpoint.id).where(
                LocalEndpoint.user_id == user_id,
                LocalEndpoint.name == name,
            )
            return await self.session.execute(existing_stmt).scalar_one()
        except:
            raise ValueError

    async def get_user_local_endpoints(self, user_id):
        query = select(LocalEndpoint.name).where(LocalEndpoint.user_id == user_id)
        raw_local_endpoints = await self.session.execute(query)
        return list(raw_local_endpoints.fetchall())

    async def filter(self, user_id, name):
        query = (
            select(LocalEndpoint)
            .where(LocalEndpoint.user_id == user_id)
            .where(LocalEndpoint.name == name)
        )
        raw_custom_endpoints = await self.session.execute(query)
        return list(raw_custom_endpoints.scalars().fetchall())
