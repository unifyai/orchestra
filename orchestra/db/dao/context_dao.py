import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Context,
    ContextHistory,
    JSONLog,
    Log,
    LogEvent,
    LogEventContext,
    LogHistory,
)


def delete_orphaned_log_events(session: Session, project_id: int) -> None:
    # Using a scoped delete for the specific project.
    # This statement deletes log events that have no association rows in log_event_context.
    session.execute(
        text(
            """
        DELETE FROM log_event le
        WHERE le.project_id = :project_id
          AND NOT EXISTS (
            SELECT 1
            FROM log_event_context lec
            WHERE lec.log_event_id = le.id
          );
        """,
        ),
        {"project_id": project_id},
    )
    session.commit()


class ContextDAO:
    def __init__(self, session: Session):
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
                # check if name is valid
                if not re.match(r"^[a-zA-Z0-9_/]+$", name):
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
            self.archive_context_state(
                context,
                name=context.name,
                description="Auto-commit from log modification",
            )

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
            delete_orphaned_log_events(self.session, context.project_id)
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

    def get_context_id(self, project_id: int, body):
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

    def add_logs_copy(self, context_id: int, log_ids: List[int]) -> None:
        """Associate copies of LogEvent instances with the specified context.

        This method creates new copies of the specified log events and their associated
        Log and JSONLog entries, then associates these copies with the context.

        Args:
            context_id: ID of the context to associate logs with
            log_ids: List of log event IDs to copy and associate with the context

        Raises:
            ValueError: If context_id doesn't exist or any log_ids don't exist
            ValueError: If duplicates are found and context doesn't allow duplicates
        """
        try:
            # Get the context to check if duplicates are allowed
            context = self.session.query(Context).filter_by(id=context_id).one_or_none()
            if not context:
                raise ValueError(f"Context with id {context_id} not found")

            # Get current timestamp for all new records
            current_time = datetime.now(timezone.utc)

            # Process each log event
            for original_log_id in log_ids:
                # Query the original LogEvent
                original_log_event = (
                    self.session.query(LogEvent)
                    .filter_by(id=original_log_id)
                    .one_or_none()
                )
                if not original_log_event:
                    raise ValueError(f"Log event with id {original_log_id} not found")

                # Check for duplicates if the context doesn't allow them
                if not context.allow_duplicates:
                    if self.check_for_duplicates(context_id, original_log_event.id):
                        raise ValueError(
                            f"Duplicate log entry detected. Context '{context.name}' does not allow duplicates.",
                        )

                # Create a new LogEvent by copying necessary fields
                new_log_event = LogEvent(
                    project_id=original_log_event.project_id,
                    created_at=current_time,
                    updated_at=current_time,
                )
                self.session.add(new_log_event)
                self.session.flush()  # Get the new ID

                # Query all associated Log rows for the original log event
                original_logs = (
                    self.session.query(Log)
                    .filter_by(log_event_id=original_log_id)
                    .all()
                )

                # Prepare bulk insert for Log entries
                new_logs = []
                for original_log in original_logs:
                    new_log = Log(
                        log_event_id=new_log_event.id,
                        key=original_log.key,
                        value=original_log.value,
                        version=original_log.version,
                        inferred_type=original_log.inferred_type,
                    )
                    new_logs.append(new_log)

                # Bulk insert all new Log entries
                if new_logs:
                    self.session.bulk_save_objects(new_logs)

                # Check for JSONLog entries (if the model exists)
                if JSONLog is not None:
                    try:
                        # Query JSONLog entries for the original log event
                        original_json_logs = (
                            self.session.query(JSONLog)
                            .filter_by(log_event_id=original_log_id)
                            .all()
                        )

                        # Prepare bulk insert for JSONLog entries
                        new_json_logs = []
                        for original_json_log in original_json_logs:
                            new_json_log = JSONLog(
                                log_event_id=new_log_event.id,
                                key=original_json_log.key,
                                value=original_json_log.value,
                                version=original_json_log.version,
                            )
                            new_json_logs.append(new_json_log)

                        # Bulk insert all new JSONLog entries
                        if new_json_logs:
                            self.session.bulk_save_objects(new_json_logs)
                    except Exception:
                        pass

                # Create association between the new log event and context
                association = LogEventContext(
                    log_event_id=new_log_event.id,
                    context_id=context_id,
                )
                self.session.add(association)

            # Increment version if context is versioned
            if context.is_versioned:
                self.increment_version(context_id)

            # Commit all changes
            self.session.commit()

        except Exception as e:
            self.session.rollback()
            raise e

    def archive_context_state(
        self,
        context: Context,
        name: str,
        description: str,
        commit_hash: Optional[str] = None,
        commit_message: Optional[str] = None,
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
            commit_hash=commit_hash,
            commit_message=commit_message,
        )
        self.session.add(history)
        self.session.flush()

    def commit(
        self,
        context_id: int,
        commit_hash: str,
        commit_message: Optional[str] = None,
    ) -> None:
        """
        Create a new version of a context, linked to a project commit.

        Args:
            context_id: The ID of the context to commit.
            commit_hash: The commit hash from the project version.
            commit_message: An optional message for the commit.
        """
        context = self.session.query(Context).filter_by(id=context_id).one_or_none()
        if context and context.is_versioned:
            self.archive_context_state(
                context,
                name=context.name,
                description=context.description,
                commit_hash=commit_hash,
                commit_message=commit_message,
            )
            context.version += 1
            context.updated_at = datetime.now(timezone.utc)

    def rollback(
        self,
        context_id: int,
        version: Optional[int] = None,
        commit_hash: Optional[str] = None,
    ) -> None:
        """
        Rollback a context to a specific version.

        Args:
            context_id: The ID of the context to rollback.
            version: The version number to rollback to.
            commit_hash: The commit hash to rollback to.
        """
        context = self.session.query(Context).filter_by(id=context_id).one_or_none()
        if not context or not context.is_versioned:
            raise ValueError("Context is not versioned.")

        history_query = self.session.query(ContextHistory).filter_by(
            context_id=context_id,
        )
        if version:
            history_query = history_query.filter_by(version=version)
        elif commit_hash:
            history_query = history_query.filter_by(commit_hash=commit_hash)
        else:
            raise ValueError("Either version or commit_hash must be provided.")

        context_history = history_query.one_or_none()
        if not context_history:
            raise ValueError("Specified version not found.")

        # Restore logs
        for log_event_id, log_versions in context_history.log_versions.items():
            for key, log_version in log_versions.items():
                log_history = (
                    self.session.query(LogHistory)
                    .filter_by(
                        log_event_id=int(log_event_id),
                        key=key,
                        version=log_version,
                    )
                    .one_or_none()
                )
                if log_history:
                    # Find the current log and update it
                    current_log = (
                        self.session.query(Log)
                        .filter_by(log_event_id=int(log_event_id), key=key)
                        .one_or_none()
                    )
                    if current_log:
                        current_log.value = log_history.value
                        current_log.inferred_type = log_history.inferred_type
                        current_log.updated_at = datetime.now(timezone.utc)
                    else:
                        # If log was deleted, recreate it
                        new_log = Log(
                            log_event_id=int(log_event_id),
                            key=key,
                            value=log_history.value,
                            version=log_version,
                            inferred_type=log_history.inferred_type,
                        )
                        self.session.add(new_log)

        # Update context version
        context.version = context_history.version
        context.updated_at = datetime.now(timezone.utc)
        self.session.commit()
