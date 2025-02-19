from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Context,
    ContextHistory,
    Log,
    LogEvent,
    LogEventContext,
)
from orchestra.web.api.context.schema import ContextCreateRequest


class ContextDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
        is_versioned: bool = False,
    ) -> int:
        """Create a new context using upsert to handle race conditions."""
        ts = datetime.now(timezone.utc)

        stmt = pg_insert(Context).values(
            project_id=project_id,
            name=name,
            description=description,
            created_at=ts,
            updated_at=ts,
            is_versioned=is_versioned,
            version=1,
        )

        # On conflict, do nothing and return the existing context's id
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["project_id", "name"],
        ).returning(Context.id)

        result = self.session.execute(stmt)
        context_id = result.scalar()

        if context_id is None:
            # If insert failed due to conflict, retrieve the existing context
            context = self.filter(project_id=project_id, name=name)
            if context:
                context_id = context[0][0].id
            else:
                raise ValueError(f"Failed to create or retrieve context {name}")

        self.session.commit()
        return context_id

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

    def increment_version(self, id: int) -> None:
        """Increment the version of a context if it is versioned."""
        context = self.session.query(Context).filter_by(id=id).one_or_none()
        if context and context.is_versioned:
            # 1) Archive current state before incrementing
            self.archive_context_state(context)

            # 2) Now increment
            context.version += 1
            context.updated_at = datetime.now(timezone.utc)
            self.session.commit()

    def delete(self, id: int) -> None:
        try:
            context = self.session.query(Context).filter_by(id=id).one()
            print("Before Deleting context")
            query = select(LogEventContext).where(LogEventContext.context_id == id)
            rows = self.session.execute(query)
            print(rows.fetchall())
            print(len(rows.fetchall()))

            self.session.delete(context)

            self.session.flush()
            # Find orphaned LogEvents
            orphaned_events = (
                self.session.query(LogEvent)
                .filter(~LogEvent.contexts.any())  # no associated contexts
                .all()
            )
            print("After Deleting context")
            query = select(LogEventContext).where(LogEventContext.context_id == id)
            rows = self.session.execute(query)
            print(rows.fetchall())
            print(len(rows.fetchall()))
            # Delete the orphaned log events
            for orphan in orphaned_events:
                self.session.delete(orphan)

            self.session.commit()
        except Exception:
            self.session.rollback()
            raise ValueError(f"Failed to delete context with id {id}")

    def get_or_create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
        is_versioned: bool = False,
    ) -> int:
        """Get or create a context using upsert."""
        return self.create(
            project_id=project_id,
            name=name,
            description=description,
            is_versioned=is_versioned,
        )

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

            # Increment version if context is versioned
            self.increment_version(context_id)

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise e

    def is_versioned(self, context_id: int) -> bool:
        context = self.session.query(Context).filter_by(id=context_id).one_or_none()
        return context and context.is_versioned

    def get_context_id(self, project_id: int, body: ContextCreateRequest):
        if body:
            return self.get_or_create(
                project_id=project_id,
                name=body.name,
                description=body.description,
                is_versioned=body.is_versioned,
            )
        else:
            # Create or get default context using upsert
            return self.get_or_create(
                project_id=project_id,
                name="default",
                description="default context",
                is_versioned=False,
            )

    def build_log_versions_map(self, context: Context) -> Dict[str, Dict[str, int]]:
        """
        For each log_event in the context, gather each log key and store
        the current context.version as that log's version. We store a map:

            {
                "<log_event_id>": {
                    "<field_key>": <context.version>,
                    ...
                },
                ...
            }
        """
        result = {}
        for le in context.log_events:
            log_rows = self.session.query(Log).filter_by(log_event_id=le.id).all()
            # we store context.version as the version integer for each key in that log
            row_map = {}
            for row in log_rows:
                row_map[row.key] = context.version
            if row_map:
                result[str(le.id)] = row_map

        return result

    def archive_context_state(
        self,
        context: Context,
        name: str,
        description: str,
    ) -> None:
        """Archive the current state of a context in ContextHistory."""
        if not context.is_versioned:
            return

        # build the log_versions map for all logs in this context
        current_log_versions = self.build_log_versions_map(context)

        history = ContextHistory(
            context_id=context.id,
            version=context.version,
            name=name,
            description=description,
            log_versions=current_log_versions,
            archived_at=datetime.now(timezone.utc),
        )
        self.session.add(history)
        self.session.flush()  # so we get an ID
        self.session.commit()
