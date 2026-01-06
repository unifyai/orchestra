"""Async version of custom_router_dao for use with AsyncSession."""

from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import CustomRouter


class AsyncCustomRouterDAO:
    """Class for accessing custom router table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_custom_router(
        self,
        user_id: str,
        router_name: str,
        router_id: str,
    ) -> None:
        self.session.add(
            CustomRouter(user_id=user_id, router_name=router_name, router_id=router_id),
        )

    async def get_router_id(self, user_id: str, router_name) -> List[CustomRouter]:
        query = (
            select(CustomRouter)
            .where(CustomRouter.router_name == router_name)
            .where((CustomRouter.user_id == user_id) | (CustomRouter.user_id == None))
        )

        raw_custom_routers = await self.session.execute(query)
        return list(raw_custom_routers.scalars().fetchall())
