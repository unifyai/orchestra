"""Async version of dashboard_view_dao for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import DashboardView


class AsyncDashboardViewDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, project_id: int, name: str, view: str) -> None:
        self.session.add(
            DashboardView(
                project_id=project_id,
                name=name,
                view=view,
            ),
        )

    async def filter(
        self,
        id: Optional[int] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
        view: Optional[str] = None,
    ) -> List[DashboardView]:
        query = select(DashboardView)
        if id:
            query = query.where(DashboardView.id == id)
        if project_id:
            query = query.where(DashboardView.project_id == project_id)
        if name:
            query = query.where(DashboardView.name == name)
        if view:
            query = query.where(DashboardView.view == view)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())

    async def update(self, id: int, name: Optional[str] = None) -> None:
        query = select(DashboardView)
        query = query.where(DashboardView.id == id)
        raw = await self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)

    async def rename(self, project_id, name, new_name):
        dashboard_view_id = self.filter(project_id=project_id, name=name)[0].id
        self.update(id=dashboard_view_id, name=new_name)

    async def list_dashboard_views(self, project_id: int):
        query = (
            select(DashboardView.name, DashboardView.view)
            .where(DashboardView.project_id == project_id)
            .order_by(DashboardView.created_at)
        )
        rows = await self.session.execute(query)
        return rows.fetchall()

    async def delete(self, id: int):
        try:
            dashboard_view = (
                (await self.session.execute(select(DashboardView).filter_by(id=id)))
                .scalars()
                .one()
            )
            await self.session.delete(dashboard_view)
            await self.session.commit()
        except:
            await self.session.rollback()
            raise ValueError
