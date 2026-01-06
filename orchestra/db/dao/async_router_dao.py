"""Async version of router_dao for use with AsyncSession."""

from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Router


class AsyncRouterDAO:
    """Class for accessing trained routers"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        user_id: str,
        name: str,
        endpoints: list[str],
        evaluator_id: int,
    ) -> int:
        new_router = Router(
            user_id=user_id,
            name=name,
            endpoints=endpoints,
            evaluator_id=evaluator_id,
        )
        self.session.add(new_router)
        await self.session.flush()
        return new_router.id

    async def update(
        self,
        id: int,
        trained: Optional[bool] = None,
        gcp_router_id: Optional[int] = None,
        deployed: Optional[bool] = None,
    ):
        query = select(Router).where(Router.id == id)
        raw = await self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if trained:
                setattr(entry, "trained", trained)
            if gcp_router_id:
                setattr(entry, "gcp_router_id", gcp_router_id)
            if deployed:
                setattr(entry, "deployed", deployed)

    async def filter(self, user_id: Optional[str] = None, name: Optional[str] = None):
        query = select(Router)
        if user_id:
            query = query.where(Router.user_id == user_id)
        if name:
            query = query.where(Router.name == name)

        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())

    async def rename(self, user_id, name, new_name):
        query = (
            select(Router).where(Router.user_id == user_id).where(Router.name == name)
        )
        raw = await self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            setattr(entry, "name", new_name)

    async def delete(self, user_id, name):
        query = (
            delete(Router).where(Router.user_id == user_id).where(Router.name == name)
        )
        await self.session.execute(query)
