import hashlib
import re
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Context,
    ContextVersion,
    JSONLog,
    Log,
    LogEvent,
    LogEventContext,
    LogVersion,
    ProjectVersion,
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


class ContextDAO:
    def __init__(self, session: Session):
        self.session = session

    def _validate_description(self, description: Optional[str]) -> None:
        """Validate description length."""
        if description is not None and len(description) > 256:
            raise ValueError("Description cannot exceed 256 characters")

    def create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
        is_versioned: bool = False,
        allow_duplicates: bool = True,
        unique_column_ids: Optional[List[str]] = None,
    ) -> int:
        """Create a new context using upsert to handle race conditions."""
        from orchestra.db.dao.field_type_dao import FieldTypeDAO

        ts = datetime.now(timezone.utc)

        self._validate_description(description)

        stmt = pg_insert(Context).values(
            project_id=project_id,
            name=name,
            description=description,
            created_at=ts,
            updated_at=ts,
            is_versioned=is_versioned,
            allow_duplicates=allow_duplicates,
            unique_id_names=unique_column_ids or [],
        )

        # On conflict, do nothing and return the existing context's id
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["project_id", "name"],
        ).returning(Context.id)

        result = self.session.execute(stmt)
        context_id = result.scalar()

        if context_id is None:
            # If insert failed due to conflict, retrieve the existing context
            context_raw = self.filter(project_id=project_id, name=name)
            if context_raw:
                context_id = context_raw[0][0].id
            else:
                raise ValueError(f"Failed to create or retrieve context {name}")

        # If unique_column_ids is provided, ensure the FieldType exists
        if unique_column_ids:
            field_type_dao = FieldTypeDAO(self.session)

            for id_name in unique_column_ids:
                field_type = field_type_dao.get_by_name_and_context(
                    project_id,
                    id_name,
                    context_id,
                )
                if not field_type:
                    # Create the field type for the sequential ID
                    field_type_dao.create_field_type_if_absent(
                        project_id=project_id,
                        field_name=id_name,
                        value=0,  # for type inference to integer
                        context_id=context_id,
                        field_category="entry",
                        mutable=False,
                        unique=True,
                        description=f"Unique sequential ID component.",
                    )
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

    def delete(self, id: int) -> None:
        from orchestra.db.dao.log_dao import LogDAO

        try:
            context = self.session.query(Context).filter_by(id=id).one()

            # Delete associated GCS media BEFORE deleting the context
            log_dao = LogDAO(self.session, self)
            log_events_subquery = (
                select(LogEvent.id)
                .join(LogEventContext)
                .where(LogEventContext.context_id == id)
                .subquery()
            )
            logs_to_delete_query = self.session.query(Log).filter(
                Log.log_event_id.in_(select(log_events_subquery.c.id)),
            )
            log_dao._bulk_delete_gcs_media(logs_to_delete_query)

            # Proceed with deleting the context from the database
            self.session.delete(context)
            self.session.flush()  # Ensure the context deletion cascades.

            # then remove all orphaned log events
            delete_orphaned_log_events(self.session, context.project_id)
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete context with id {id}: {e}")

    def get_or_create(
        self,
        project_id: int,
        name: str,
        description: Optional[str] = None,
        is_versioned: bool = False,
        allow_duplicates: bool = True,
        unique_column_ids: Optional[List[str]] = None,
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
            self._validate_description(description)
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
                allow_duplicates=allow_duplicates,
                unique_id_names=unique_column_ids or [],
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
                            allow_duplicates=allow_duplicates,
                            unique_id_names=unique_column_ids or [],
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
                    unique_column_ids=unique_column_ids,
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
            unique_column_ids = getattr(body, "unique_column_ids", None)
            return self.get_or_create(
                project_id=project_id,
                name=body.name,
                description=body.description,
                is_versioned=body.is_versioned,
                allow_duplicates=allow_duplicates,
                unique_column_ids=unique_column_ids,
            )
        else:
            # Create or get default context using upsert
            return self.get_or_create(
                project_id=project_id,
                name="",
                description="default context",
                is_versioned=False,
                unique_column_ids=None,
            )

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
                        param_version=original_log.param_version,
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
            # Commit all changes
            self.session.commit()

        except Exception as e:
            self.session.rollback()
            raise e

    def commit(self, context_id: int, commit_message: Optional[str] = None) -> str:
        """
        Create a new version of a single context.
        """
        context = self.session.query(Context).filter_by(id=context_id).one_or_none()
        if not context or not context.is_versioned:
            raise ValueError("Context is not versioned.")

        # Get the current HEAD commit
        current_head = context.current_commit_hash

        # If context has no commits yet, use the project's current commit as the parent
        if current_head is None and context.project:
            current_head = context.project.current_commit_hash

        # 1. Generate a unique commit hash
        commit_hash = hashlib.sha256(
            f"context_{context_id}{datetime.now(timezone.utc)}".encode(),
        ).hexdigest()

        # 2. Create a snapshot for the context
        self.create_version_snapshot(
            context=context,
            commit_hash=commit_hash,
            commit_message=commit_message,
            project_version=None,  # This is a context-only commit
            prev_commit_hash=current_head,
        )

        # Update the previous version's next_commit_hash array if it exists
        if current_head:
            # Try to find a context version first
            prev_context_version = (
                self.session.query(ContextVersion)
                .filter_by(
                    context_id=context_id,
                    commit_hash=current_head,
                )
                .with_for_update()
                .one_or_none()
            )

            if prev_context_version:
                if commit_hash not in prev_context_version.next_commit_hash:
                    prev_context_version.next_commit_hash = (
                        prev_context_version.next_commit_hash + [commit_hash]
                    )
            else:
                # If not found, it might be a project version
                prev_project_version = (
                    self.session.query(ProjectVersion)
                    .filter_by(
                        project_id=context.project_id,
                        commit_hash=current_head,
                    )
                    .with_for_update()
                    .one_or_none()
                )

                if prev_project_version:
                    # For project versions, we update the context version that was created as part of that project commit
                    context_version_in_project = (
                        self.session.query(ContextVersion)
                        .filter_by(
                            context_id=context_id,
                            project_version_id=prev_project_version.id,
                        )
                        .with_for_update()
                        .one_or_none()
                    )

                    if context_version_in_project:
                        if (
                            commit_hash
                            not in context_version_in_project.next_commit_hash
                        ):
                            context_version_in_project.next_commit_hash = (
                                context_version_in_project.next_commit_hash
                                + [commit_hash]
                            )

        context.updated_at = datetime.now(timezone.utc)

        # Update the context's HEAD pointer
        context.current_commit_hash = commit_hash

        self.session.commit()
        return commit_hash

    def rollback(self, context_id: int, commit_hash: str) -> None:
        """
        Orchestrates the rollback of a context in two phases:
        1. Restore the state from the version snapshot.
        2. Clean up any orphaned data from the previous state.
        This ensures the operation is atomic and safe.
        """
        try:
            context_version = (
                self.session.query(ContextVersion)
                .filter_by(context_id=context_id, commit_hash=commit_hash)
                .one_or_none()
            )
            if not context_version:
                raise ValueError(
                    f"Commit hash {commit_hash} not found for context {context_id}.",
                )

            context = self.session.query(Context).filter_by(id=context_id).one()

            # Phase 1: Restore the state.
            self.rollback_to_version(context_id, context_version.id)
            context.updated_at = datetime.now(timezone.utc)

            # Move the HEAD pointer to the target commit
            context.current_commit_hash = commit_hash

            self.session.commit()

            # Phase 2: Garbage collection in a new transaction.
            delete_orphaned_log_events(self.session, context.project_id)
            self.session.commit()

        except Exception as e:
            self.session.rollback()
            raise e

    def get_commit_history(self, context_id: int) -> List[dict]:
        """
        Retrieves the combined commit history for a versioned context,
        including context-only and project-level commits.
        """
        context = self.session.query(Context).filter_by(id=context_id).one_or_none()
        if not context or not context.is_versioned:
            raise ValueError("Context is not versioned.")

        # Query all versions for this context
        versions = (
            self.session.query(ContextVersion)
            .filter_by(context_id=context_id)
            .order_by(ContextVersion.archived_at.desc())
            .all()
        )

        history = []
        for v in versions:
            history.append(
                {
                    "commit_hash": v.commit_hash,
                    "commit_message": v.commit_message,
                    "created_at": v.archived_at.isoformat(),
                    "type": "project" if v.project_version_id else "context",
                    "prev_commit_hash": v.prev_commit_hash,
                    "next_commit_hash": v.next_commit_hash,
                },
            )

        return history

    def create_version_snapshot(
        self,
        context: Context,
        commit_hash: str,
        commit_message: Optional[str] = None,
        project_version: Optional[ProjectVersion] = None,
        prev_commit_hash: Optional[str] = None,
    ) -> None:
        """Creates a snapshot of the context's current state."""
        if not context.is_versioned:
            return

        # 1. Create a ContextVersion record
        context_version = ContextVersion(
            context_id=context.id,
            project_version_id=project_version.id if project_version else None,
            name=context.name,
            description=context.description,
            commit_hash=commit_hash,
            commit_message=commit_message,
            prev_commit_hash=prev_commit_hash,
        )
        self.session.add(context_version)
        self.session.flush()  # Flush to get the context_version.id

        # Update the previous version's next_commit_hash array if it exists
        if prev_commit_hash:
            prev_version = (
                self.session.query(ContextVersion)
                .filter_by(
                    context_id=context.id,
                    commit_hash=prev_commit_hash,
                )
                .with_for_update()
                .one()
            )
            if commit_hash not in prev_version.next_commit_hash:
                prev_version.next_commit_hash = prev_version.next_commit_hash + [
                    commit_hash,
                ]

        # 2. Get all current logs for the context
        logs_to_version = (
            self.session.query(Log)
            .join(LogEvent, Log.log_event_id == LogEvent.id)
            .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
            .filter(LogEventContext.context_id == context.id)
            .all()
        )

        if not logs_to_version:
            return

        # 3. Create a snapshot of each log
        log_versions = [
            LogVersion(
                context_version_id=context_version.id,
                log_event_id=log.log_event_id,
                key=log.key,
                value=log.value,
                param_version=log.param_version,
                inferred_type=log.inferred_type,
                created_at=log.created_at,
                updated_at=log.updated_at,
            )
            for log in logs_to_version
        ]

        # 4. Bulk insert the log snapshots for efficiency
        self.session.bulk_save_objects(log_versions)

    def rollback_to_version(self, context_id: int, context_version_id: int) -> None:
        """
        Helper method to prepare the rollback.
        This method only prepares the operations and does NOT commit.
        """
        log_versions_to_restore = (
            self.session.query(LogVersion)
            .filter_by(context_version_id=context_version_id)
            .all()
        )
        context = self.session.query(Context).filter_by(id=context_id).one()

        self.session.query(LogEventContext).filter_by(context_id=context_id).delete(
            synchronize_session=False,
        )

        grouped_lvs = {}
        if log_versions_to_restore:
            for lv in log_versions_to_restore:
                grouped_lvs.setdefault(lv.log_event_id, []).append(lv)

        for original_log_event_id, lvs in grouped_lvs.items():
            new_log_event = LogEvent(project_id=context.project_id)
            self.session.add(new_log_event)
            self.session.flush()

            self.session.add(
                LogEventContext(log_event_id=new_log_event.id, context_id=context_id),
            )

            new_logs = []
            new_json_logs = []
            for lv in lvs:
                new_logs.append(
                    Log(
                        log_event_id=new_log_event.id,
                        key=lv.key,
                        value=lv.value,
                        param_version=lv.param_version,
                        inferred_type=lv.inferred_type,
                        created_at=lv.created_at,
                        updated_at=lv.updated_at,
                    ),
                )
                if isinstance(lv.value, (dict, list)):
                    new_json_logs.append(
                        JSONLog(
                            log_event_id=new_log_event.id,
                            key=lv.key,
                            value=lv.value,
                        ),
                    )
            if new_logs:
                self.session.bulk_save_objects(new_logs)
            if new_json_logs:
                self.session.bulk_save_objects(new_json_logs)
