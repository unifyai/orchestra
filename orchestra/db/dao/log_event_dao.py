from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import LogEvent


class LogEventDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        project_id: int,
    ) -> None:

        self.session.add(
            LogEvent(
                project_id=project_id,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        project_id: Optional[int] = None,
    ) -> List[LogEvent]:
        query = select(LogEvent)
        if id:
            query = query.where(LogEvent.id == id)
        if project_id:
            query = query.where(LogEvent.project_id == project_id)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        project_id: Optional[int] = None,
    ) -> None:
        query = select(LogEvent)
        query = query.where(LogEvent.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if project_id:
                setattr(entry, "project_id", project_id)

    def delete(self, id: int):
        try:
            api_key = self.session.query(LogEvent).filter_by(id=id).one()
            self.session.delete(api_key)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
