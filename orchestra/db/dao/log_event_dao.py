from datetime import datetime, timezone
from typing import List, Optional, Union

from sqlalchemy import Integer, and_, cast, func, select
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.models.orchestra_models import (
    Context,
    Log,
    LogEvent,
    LogEventContext,
    Project,
)


class LogEventDAO:
    def __init__(self, session: Session):
        self.session = session

    def bulk_create(
        self,
        project_id: int,
        count: int,
        context_id: Optional[int] = None,
    ) -> List[int]:
        """Create multiple LogEvent instances in one operation."""
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
        self.session.flush()  # Flush to get IDs before committing

        log_event_ids = [event.id for event in log_events]

        if context_id:
            # Associate logs with context
            associations = [
                LogEventContext(
                    log_event_id=log_event_id,
                    context_id=context_id,
                )
                for log_event_id in log_event_ids
            ]
            self.session.add_all(associations)

            # Check if this context needs a unique sequential ID
            context = self.session.query(Context).filter_by(id=context_id).one()
            if context.unique_id_column:
                field_type_dao = FieldTypeDAO(self.session)
                field_type = field_type_dao.get_by_name_and_context(
                    project_id,
                    context.unique_id_name,
                    context.id,
                )

                if field_type:
                    # Create sequential ID log entries
                    log_dao = LogDAO(self.session, ContextDAO(self.session))

                    # Get the current max ID before creating any new ones
                    # This ensures proper sequencing even across multiple API calls
                    current_max_id = (
                        self.session.query(
                            func.coalesce(func.max(cast(Log.value, Integer)), 0),
                        )
                        .join(LogEvent, Log.log_event_id == LogEvent.id)
                        .join(
                            LogEventContext,
                            LogEvent.id == LogEventContext.log_event_id,
                        )
                        .filter(
                            and_(
                                LogEventContext.context_id == context_id,
                                Log.key == context.unique_id_name,
                            ),
                        )
                        .scalar()
                    ) or 0

                    # Create sequential IDs for all log events in this batch
                    sequential_id_logs = []
                    for i, log_event_id in enumerate(log_event_ids):
                        new_id = current_max_id + i + 1
                        sequential_id_logs.append(
                            {
                                "project_id": project_id,
                                "log_event_id": log_event_id,
                                "key": context.unique_id_name,
                                "value": new_id,
                                "context_id": context_id,
                                "explicit_types": {
                                    context.unique_id_name: {"type": "int"},
                                },
                            },
                        )

                    # Create all sequential ID logs in one batch
                    if sequential_id_logs:
                        log_dao.bulk_create(sequential_id_logs)

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
