from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.models.orchestra_models import (
    Context,
    Log,
    LogEvent,
    LogEventContext,
    LogEventLog,
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
        return_row_ids: bool = False,
        provided_unique_ids: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[List[int], tuple[List[int], List[Any]]]:
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
        row_ids: List[Any] = [None] * count

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
            if context.unique_id_names:
                log_dao = LogDAO(self.session, ContextDAO(self.session))
                unique_id_names = context.unique_id_names or []

                if len(unique_id_names) > 1:
                    # Nested unique IDs
                    if provided_unique_ids is None:
                        provided_unique_ids = [{} for _ in range(count)]
                    reserved_ids = log_dao.get_next_nested_ids(
                        project_id=project_id,
                        context_id=context_id,
                        columns=unique_id_names,
                        provided_ids=provided_unique_ids,
                    )
                    row_ids = reserved_ids

                    # Create log entries for all unique ID columns
                    all_id_logs = []
                    for i, log_event_id in enumerate(log_event_ids):
                        id_dict = row_ids[i]
                        for col_name, col_value in id_dict.items():
                            all_id_logs.append(
                                {
                                    "project_id": project_id,
                                    "log_event_id": log_event_id,
                                    "key": col_name,
                                    "value": col_value,
                                    "context_id": context_id,
                                    "explicit_types": {col_name: {"type": "int"}},
                                },
                            )
                    if all_id_logs:
                        log_dao.bulk_create(all_id_logs)

                elif len(unique_id_names) == 1:
                    # Single unique ID
                    param_key = unique_id_names[0]
                    reserved_ids = log_dao.get_next_row_ids(
                        project_id=project_id,
                        context_id=context_id,
                        param_key=param_key,
                        count=count,
                    )
                    row_ids = reserved_ids

                    # Create sequential ID log entries
                    sequential_id_logs = []
                    for i, log_event_id in enumerate(log_event_ids):
                        new_id = row_ids[i]
                        sequential_id_logs.append(
                            {
                                "project_id": project_id,
                                "log_event_id": log_event_id,
                                "key": param_key,
                                "value": new_id,
                                "context_id": context_id,
                                "explicit_types": {param_key: {"type": "int"}},
                            },
                        )
                    if sequential_id_logs:
                        log_dao.bulk_create(
                            sequential_id_logs,
                        )

        self.session.flush()
        if return_row_ids:
            return (log_event_ids, row_ids)
        else:
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
        ids = id if isinstance(id, list) else [id]
        if not ids:
            return

        try:
            # Delete associated GCS media BEFORE deleting DB records
            log_dao = LogDAO(self.session, ContextDAO(self.session))
            logs_to_delete_query = (
                self.session.query(Log)
                .join(
                    LogEventLog,
                    LogEventLog.log_id == Log.id,
                )
                .filter(
                    LogEventLog.log_event_id.in_(ids),
                )
            )
            log_dao._bulk_delete_gcs_media(logs_to_delete_query)

            # First, delete the association rows referencing these log events
            self.session.query(LogEventContext).filter(
                LogEventContext.log_event_id.in_(ids),
            ).delete(synchronize_session=False)

            # Then, delete the log event(s) themselves (which cascades to Log and JSONLog in the DB)
            self.session.query(LogEvent).filter(
                LogEvent.id.in_(ids),
            ).delete(synchronize_session=False)

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete log events: {e}")

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
