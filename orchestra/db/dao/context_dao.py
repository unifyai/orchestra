from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Context, LogEvent, LogEventContext


class ContextDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
    ) -> int:
        ts = datetime.now(timezone.utc)
        new_context = Context(
            project_id=project_id,
            name=name,
            description=description,
            created_at=ts,
            updated_at=ts,
        )

        self.session.add(new_context)
        self.session.commit()
        return new_context.id

    def filter(
        self,
        id: Optional[int] = None,
        project_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> List[Context]:
        query = select(Context)

        if id:
            query = query.where(Context.id == id)
        if project_id:
            query = query.where(Context.project_id == project_id)
        if name:
            query = query.where(Context.name == name)

        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        query = select(Context)
        query = query.where(Context.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()

        if entry is not None:
            if name:
                if not all(c.isalnum() or c == "/" for c in name):
                    raise ValueError(
                        "Context name must contain only alphanumeric characters and '/'",
                    )
                setattr(entry, "name", name)
            if description is not None:  # Allow setting description to None
                setattr(entry, "description", description)
            self.session.commit()
        else:
            raise ValueError(f"Context with id {id} not found")

    def delete(self, id: int) -> None:
        try:
            context = self.session.query(Context).filter_by(id=id).one()
            self.session.delete(context)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise ValueError(f"Failed to delete context with id {id}")

    def get_or_create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
    ) -> Context:
        """Get or create a context.

        Args:
            project_id: ID of the project to create the context in
            name: Name of the context to create

        Returns:
            Context: The created or existing context
        """
        context = self.filter(project_id=project_id, name=name)
        if context:
            return context[0][0]
        return self.create(project_id=project_id, name=name, description=description)

    def add_logs(self, context_id: int, log_ids: List[int]) -> None:
        """Associate LogEvent instances with the specified context.

        Args:
            context_id: ID of the context to associate logs with
            log_ids: List of log event IDs to associate with the context

        Raises:
            ValueError: If context_id doesn't exist or any log_ids don't exist
        """
        try:
            # Get all log events
            log_events = (
                self.session.query(LogEvent).filter(LogEvent.id.in_(log_ids)).all()
            )
            found_ids = {log.id for log in log_events}
            missing_ids = set(log_ids) - found_ids

            if missing_ids:
                raise ValueError(f"Log events with ids {missing_ids} not found")

            # Create associations between log events and context
            for log_event in log_events:
                association = LogEventContext(
                    log_event_id=log_event.id,
                    context_id=context_id,
                )
                self.session.add(association)

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise e
