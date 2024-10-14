from typing import List, Optional, Union

from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import LogEvent, Project


class LogEventDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        project_id: int,
    ) -> Optional[int]:

        new_log_event = LogEvent(
            project_id=project_id,
        )

        self.session.add(new_log_event)
        self.session.commit()
        return new_log_event.id

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

    def delete(self, id: Union[int, List[int]]):
        id = id if isinstance(id, list) else [id]
        try:
            log_events = self.session.query(LogEvent).where(
                or_(*[LogEvent.id == i for i in id]),
            )
            [self.session.delete(le) for le in log_events]
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    def get_user_id(self, id: int) -> Optional[str]:
        query = (
            select(Project.user_id)
            .join(LogEvent, Project.id == LogEvent.project_id)
            .where(LogEvent.id == id)
        )
        rows = self.session.execute(query).fetchone()
        return rows[0] if rows is not None else None
