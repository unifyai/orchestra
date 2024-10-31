from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DashboardView


class DashboardViewDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(self, project_id: int, name: str, view: str) -> None:
        self.session.add(
            DashboardView(
                project_id=project_id,
                name=name,
                view=view,
            ),
        )

    def filter(
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
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update(self, id: int, name: Optional[str] = None) -> None:
        query = select(DashboardView)
        query = query.where(DashboardView.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)

    def rename(self, project_id, name, new_name):
        dashboard_view_id = self.filter(project_id=project_id, name=name)[0].id
        self.update(id=dashboard_view_id, name=new_name)

    def list_dashboard_views(self, project_id: int):
        query = (
            select(DashboardView.name, DashboardView.view)
            .where(DashboardView.project_id == project_id)
            .order_by(DashboardView.created_at)
        )
        rows = self.session.execute(query)
        return rows.fetchall()

    def delete(self, id: int):
        try:
            dashboard_view = self.session.query(DashboardView).filter_by(id=id).one()
            self.session.delete(dashboard_view)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
