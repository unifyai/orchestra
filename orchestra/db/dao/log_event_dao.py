from datetime import datetime, timezone
from typing import List, Optional, Union

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import LogEvent, LogEventContext, Project


class LogEventDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        project_id: int,
        context_id: Optional[int] = None,
    ) -> Optional[int]:

        ts = datetime.now(timezone.utc)
        new_log_event = LogEvent(
            project_id=project_id,
            created_at=ts,
            updated_at=ts,
        )

        self.session.add(new_log_event)
        self.session.commit()

        if context_id:
            association = LogEventContext(
                log_event_id=new_log_event.id,
                context_id=context_id,
            )
            self.session.add(association)
            self.session.commit()

        return new_log_event.id

    def bulk_create(
        self,
        project_id: int,
        count: int,
        context_id: Optional[int] = None,
    ) -> List[int]:
        """Create multiple LogEvent instances in one operation.

        Args:
            project_id: The project ID to associate with the log events
            count: Number of log events to create
            context_id: Optional context ID to associate with the log events

        Returns:
            A list of created log event IDs
        """
        ts = datetime.now(timezone.utc)
        log_events = [
            LogEvent(
                project_id=project_id,
                created_at=ts,
                updated_at=ts,
            )
            for _ in range(count)
        ]

        self.session.add_all(log_events)
        self.session.commit()

        log_event_ids = [event.id for event in log_events]

        if context_id:
            associations = [
                LogEventContext(
                    log_event_id=log_event_id,
                    context_id=context_id,
                )
                for log_event_id in log_event_ids
            ]
            self.session.add_all(associations)
            self.session.commit()

        return log_event_ids

    def filter(
        self,
        id: Optional[int] = None,
        project_id: Optional[int] = None,
        context_id: Optional[int] = None,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> List[LogEvent]:
        query = select(LogEvent).distinct()

        if id:
            query = query.where(LogEvent.id == id)
        if project_id:
            query = query.where(LogEvent.project_id == project_id)
        if context_id:
            query = query.join(LogEventContext).where(
                LogEventContext.context_id == context_id,
            )

        query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        query = query.order_by(LogEvent.created_at)

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
            # First, delete the association rows referencing these log events
            self.session.query(LogEventContext).filter(
                LogEventContext.log_event_id.in_(id),
            ).delete(synchronize_session=False)
            # Then, delete the log event(s)
            self.session.query(LogEvent).filter(
                LogEvent.id.in_(id),
            ).delete(synchronize_session=False)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    def get_ts(self, id: int) -> Optional[datetime]:
        query = (
            select(Project.created_at)
            .join(LogEvent, Project.id == LogEvent.project_id)
            .where(LogEvent.id == id)
        )
        rows = self.session.execute(query).fetchone()
        return rows[0] if rows is not None else None

    def get_user_id(self, id: int) -> Optional[str]:
        query = (
            select(Project.user_id)
            .join(LogEvent, Project.id == LogEvent.project_id)
            .where(LogEvent.id == id)
        )
        rows = self.session.execute(query).fetchone()
        return rows[0] if rows is not None else None

    def get_user_and_project_id(self, id: int) -> Optional[str]:
        query = (
            select(Project.user_id, Project.id)
            .join(LogEvent, Project.id == LogEvent.project_id)
            .where(LogEvent.id == id)
        )
        rows = self.session.execute(query).fetchone()
        return rows if rows is not None else (None, None)
