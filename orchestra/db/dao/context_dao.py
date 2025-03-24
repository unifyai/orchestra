from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from fastapi import Depends
from sqlalchemy import select, text
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


def delete_orphaned_log_events(session: Session) -> None:
    # Using a Common Table Expression (CTE) for bulk deletion.
    # This statement deletes log events that have no association rows in log_event_context.
    session.execute(
        text(
            """
        WITH orphaned AS (
            SELECT le.id
            FROM log_event le
            LEFT JOIN log_event_context lec ON le.id = lec.log_event_id
            WHERE lec.log_event_id IS NULL
        )
        DELETE FROM log_event
        WHERE id IN (SELECT id FROM orphaned);
        """,
        ),
    )
    session.commit()


class ContextDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
        is_versioned: bool = False,
        allow_duplicates: bool = True,
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
            allow_duplicates=allow_duplicates,
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
        if name is not None:
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
            if name is not None:
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
            self.session.delete(context)
            self.session.flush()  # Ensure the context deletion cascades.
            # then remove all orphaned log events
            delete_orphaned_log_events(self.session)
            self.session.commit()
        except Exception as e:
            print(e)
            self.session.rollback()
            raise ValueError(f"Failed to delete context with id {id}", e)

    def get_or_create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
        is_versioned: bool = False,
        allow_duplicates: bool = True,
    ) -> int:
        """
        Get or create a context using upsert.

        If the context doesn't exist, it will be created with the provided parameters.
        This method ensures a context is always returned, creating one implicitly if needed.

        Args:
            project_id: ID of the project to associate the context with
            name: Name of the context
            description: Optional description of the context
            is_versioned: Whether the context should be versioned

        Returns:
            The ID of the existing or newly created context
        """
        try:
            # First try to find the context
            contexts = self.filter(project_id=project_id, name=name)
            if contexts:
                # Context exists, return its ID
                return contexts[0][0].id

            # Context doesn't exist, create it
            ts = datetime.now(timezone.utc)

            # Use description if provided, otherwise use a default
            actual_description = (
                description if description is not None else "default context"
            )

            # Create the context
            stmt = pg_insert(Context).values(
                project_id=project_id,
                name=name,
                description=actual_description,
                created_at=ts,
                updated_at=ts,
                is_versioned=is_versioned,
                version=1,
                allow_duplicates=allow_duplicates,
            )

            # On conflict, do nothing and return the existing context's id
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["project_id", "name"],
            ).returning(Context.id)

            result = self.session.execute(stmt)
            context_id = result.scalar()

            if context_id is None:
                # If insert failed due to conflict, retrieve the existing context
                # This handles race conditions where the context was created between our check and insert
                contexts = self.filter(project_id=project_id, name=name)
                if contexts:
                    context_id = contexts[0][0].id
                else:
                    # This should rarely happen, but we'll create a default context as a fallback
                    fallback_stmt = (
                        pg_insert(Context)
                        .values(
                            project_id=project_id,
                            name=name,
                            description="default context",
                            created_at=ts,
                            updated_at=ts,
                            is_versioned=False,
                            version=1,
                            allow_duplicates=allow_duplicates,
                        )
                        .returning(Context.id)
                    )

                    fallback_result = self.session.execute(fallback_stmt)
                    context_id = fallback_result.scalar()

                    if context_id is None:
                        raise ValueError(f"Failed to create or retrieve context {name}")

            self.session.commit()
            return context_id

        except Exception as e:
            self.session.rollback()
            # As a last resort, try to create the default context
            try:
                return self.create(
                    project_id=project_id,
                    name=name,
                    description="default context",
                    is_versioned=False,
                    allow_duplicates=allow_duplicates,
                )
            except Exception:
                raise ValueError(
                    f"Failed to create or retrieve context {name}: {str(e)}",
                )

    def add_logs(self, context_id: int, log_ids: List[int]) -> None:
        """Associate LogEvent instances with the specified context.

        Args:
            context_id: ID of the context to associate logs with
            log_ids: List of log event IDs to associate with the context

        Raises:
            ValueError: If context_id doesn't exist or any log_ids don't exist
            ValueError: If duplicates are found and context doesn't allow duplicates
        """
        try:
            # Get the context to check if duplicates are allowed
            context = self.session.query(Context).filter_by(id=context_id).one_or_none()
            if not context:
                raise ValueError(f"Context with id {context_id} not found")

            # Get all log events
            log_events = (
                self.session.query(LogEvent).filter(LogEvent.id.in_(log_ids)).all()
            )
            found_ids = {log.id for log in log_events}
            missing_ids = set(log_ids) - found_ids

            if missing_ids:
                raise ValueError(f"Log events with ids {missing_ids} not found")

            # Check for duplicates if the context doesn't allow them
            if not context.allow_duplicates:
                for log_event in log_events:
                    if self.check_for_duplicates(context_id, log_event.id):
                        raise ValueError(
                            f"Duplicate log entry detected. Context '{context.name}' does not allow duplicates.",
                        )

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

    def get_context_id(self, project_id: int, body: Union[ContextCreateRequest, None]):
        if body:
            allow_duplicates = getattr(body, "allow_duplicates", True)
            return self.get_or_create(
                project_id=project_id,
                name=body.name,
                description=body.description,
                is_versioned=body.is_versioned,
                allow_duplicates=allow_duplicates,
            )
        else:
            # Create or get default context using upsert
            return self.get_or_create(
                project_id=project_id,
                name="",
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

    def check_for_duplicates(self, context_id: int, log_event_id: int) -> bool:
        """
        Check if a log event would create duplicates in the context using a single SQL query.

        Args:
            context_id: ID of the context to check
            log_event_id: ID of the log event to check for duplicates

        Returns:
            True if duplicates are found, False otherwise
        """
        query = """
        WITH new_log_pairs AS (
            SELECT key, value FROM log WHERE log_event_id = :log_event_id
        ),
        context_log_events AS (
            SELECT le.id
            FROM log_event le
            JOIN log_event_context lec ON le.id = lec.log_event_id
            WHERE lec.context_id = :context_id AND le.id != :log_event_id
        ),
        potential_duplicates AS (
            SELECT
                cle.id,
                COUNT(*) as pair_count
            FROM context_log_events cle
            JOIN log l ON cle.id = l.log_event_id
            GROUP BY cle.id
            HAVING COUNT(*) = (SELECT COUNT(*) FROM new_log_pairs)
        ),
        matching_pairs AS (
            SELECT
                pd.id,
                COUNT(*) as matching_count
            FROM potential_duplicates pd
            JOIN log l ON pd.id = l.log_event_id
            JOIN new_log_pairs nlp ON l.key = nlp.key AND l.value = nlp.value
            GROUP BY pd.id
        )
        SELECT EXISTS (
            SELECT 1 FROM matching_pairs mp
            JOIN potential_duplicates pd ON mp.id = pd.id
            WHERE mp.matching_count = pd.pair_count
        ) as has_duplicate
        """
        result = self.session.execute(
            text(query),
            {"context_id": context_id, "log_event_id": log_event_id},
        )
        return result.scalar()

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
