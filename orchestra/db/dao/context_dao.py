import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
    LogEventJSONLog,
    LogEventLog,
    LogVersion,
    ProjectVersion,
)


def delete_orphaned_log_events(session: Session, project_id: int) -> None:
    # Using a scoped delete for the specific project.
    # This statement deletes log events that have no association rows in log_event_context.
    orphaned_log_event_ids = session.execute(
        text(
            """
        SELECT le.id
        FROM log_event le
        WHERE le.project_id = :project_id
          AND NOT EXISTS (
            SELECT 1
            FROM log_event_context lec
            WHERE lec.log_event_id = le.id
          );
        """,
        ),
        {"project_id": project_id},
    ).fetchall()

    if not orphaned_log_event_ids:
        return

    orphaned_ids = [row[0] for row in orphaned_log_event_ids]

    # Delete associated logs via LogEventLog
    session.execute(
        text(
            """
        DELETE FROM log
        WHERE id IN (
            SELECT log_id FROM log_event_log
            WHERE log_event_id = ANY(:log_event_ids)
        )
        """,
        ),
        {"log_event_ids": orphaned_ids},
    )

    # Delete associated JSON logs via LogEventJSONLog
    session.execute(
        text(
            """
        DELETE FROM json_log
        WHERE id IN (
            SELECT json_log_id FROM log_event_json_log
            WHERE log_event_id = ANY(:log_event_ids)
        )
        """,
        ),
        {"log_event_ids": orphaned_ids},
    )

    # Delete associated derived logs via LogEventDerivedLog (if table exists)
    try:
        session.execute(
            text(
                """
            DELETE FROM derived_log
            WHERE id IN (
                SELECT derived_log_id FROM log_event_derived_log
                WHERE log_event_id = ANY(:log_event_ids)
            )
            """,
            ),
            {"log_event_ids": orphaned_ids},
        )
    except:
        # Table might not exist
        pass

    # Delete associations
    session.execute(
        text("DELETE FROM log_event_log WHERE log_event_id = ANY(:log_event_ids)"),
        {"log_event_ids": orphaned_ids},
    )

    session.execute(
        text("DELETE FROM log_event_json_log WHERE log_event_id = ANY(:log_event_ids)"),
        {"log_event_ids": orphaned_ids},
    )

    try:
        session.execute(
            text(
                "DELETE FROM log_event_derived_log WHERE log_event_id = ANY(:log_event_ids)",
            ),
            {"log_event_ids": orphaned_ids},
        )
    except:
        pass

    # Finally, delete the orphaned log events
    session.execute(
        text("DELETE FROM log_event WHERE id = ANY(:log_event_ids)"),
        {"log_event_ids": orphaned_ids},
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
        unique_keys: Optional[Dict[str, str]] = None,
        auto_counting: Optional[Dict[str, Optional[str]]] = None,
    ) -> int:
        """Create a new context using upsert to handle race conditions."""
        from orchestra.db.dao.field_type_dao import FieldTypeDAO

        ts = datetime.now(timezone.utc)

        self._validate_description(description)

        # Extract names and types from unique_keys dict
        unique_key_names = list(unique_keys.keys()) if unique_keys else []
        unique_key_types = list(unique_keys.values()) if unique_keys else []

        stmt = pg_insert(Context).values(
            project_id=project_id,
            name=name,
            description=description,
            created_at=ts,
            updated_at=ts,
            is_versioned=is_versioned,
            allow_duplicates=allow_duplicates,
            unique_key_names=unique_key_names,
            unique_key_types=unique_key_types,
            auto_counting=auto_counting or {},
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

        # If unique_keys is provided, ensure the FieldType exists for each column
        if unique_keys:
            field_type_dao = FieldTypeDAO(self.session)

            # Get the context to access the preserved order
            context_obj = self.session.query(Context).filter_by(id=context_id).one()
            ordered_columns = context_obj.unique_key_names or list(unique_keys.keys())

            # Ensure we iterate in the correct order
            for col_name in ordered_columns:
                if col_name not in unique_keys:
                    continue
                col_type = unique_keys[col_name]
                field_type = field_type_dao.get_by_name_and_context(
                    project_id,
                    col_name,
                    context_id,
                )
                if not field_type:
                    # Get initial value based on type
                    from orchestra.web.api.log.python2SQL.constants import (
                        get_default_value_for_type,
                    )

                    initial_value = get_default_value_for_type(col_type)

                    # Create the field type
                    # Set unique=True only for single unique keys
                    is_unique = len(unique_keys) == 1
                    field_type_dao.create_field_type_if_absent(
                        project_id=project_id,
                        field_name=col_name,
                        value=initial_value,
                        context_id=context_id,
                        field_category="entry",
                        mutable=False,  # Unique key fields should be immutable
                        unique=is_unique,  # Only set True for single unique keys
                        description=f"{'Unique' if is_unique else 'Composite unique'} key component ({col_type}).",
                    )
        self.session.commit()
        return context_id

    def bulk_create(
        self,
        project_id: int,
        contexts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Create multiple contexts in a single database transaction.

        Args:
            project_id: ID of the project to create contexts in
            contexts: List of dictionaries with context data:
                - name: str (required)
                - description: Optional[str]
                - is_versioned: bool (default False)
                - allow_duplicates: bool (default True)
                - unique_keys: Optional[Dict[str, str]]
                - auto_counting: Optional[Dict[str, Optional[str]]]

        Returns:
            Dictionary with:
                - created: List of successfully created context names
                - errors: List of errors with index, name, and error message
        """
        if not contexts:
            return {"created": [], "errors": []}

        created_contexts = []
        errors = []

        try:
            # Validate all contexts first
            for idx, context_data in enumerate(contexts):
                try:
                    name = context_data.get("name")
                    if name is None:
                        errors.append(
                            {
                                "index": idx,
                                "name": "unknown",
                                "error": "Context name is required",
                            },
                        )
                        continue

                    # Normalize name: remove leading slash to treat '/exp1/name1' the same as 'exp1/name1'
                    name = name.lstrip("/")

                    # Validate name format
                    if not re.match(r"^[a-zA-Z0-9\_\-/]+$", name) or "//" in name:
                        errors.append(
                            {
                                "index": idx,
                                "name": name,
                                "error": "Invalid context name. Names can only contain alphanumeric characters, underscores, dashes, and forward slashes. Consecutive slashes are not allowed.",
                            },
                        )
                        continue

                    # Validate description length
                    description = context_data.get("description")
                    if description is not None:
                        try:
                            self._validate_description(description)
                        except ValueError as e:
                            errors.append(
                                {
                                    "index": idx,
                                    "name": name,
                                    "error": str(e),
                                },
                            )
                            continue

                    # Check if context already exists
                    existing = self.filter(project_id=project_id, name=name)
                    if existing:
                        errors.append(
                            {
                                "index": idx,
                                "name": name,
                                "error": "A context with this name already exists in the project.",
                            },
                        )
                        continue

                except Exception as e:
                    errors.append(
                        {
                            "index": idx,
                            "name": context_data.get("name", "unknown"),
                            "error": str(e),
                        },
                    )
                    continue

            # Create all valid contexts
            for idx, context_data in enumerate(contexts):
                try:
                    name = context_data.get("name", "").lstrip("/")

                    # Skip if we already recorded an error for this context
                    if any(e["index"] == idx for e in errors):
                        continue

                    # Create the context
                    self.create(
                        project_id=project_id,
                        name=name,
                        description=context_data.get("description"),
                        is_versioned=context_data.get("is_versioned", False),
                        allow_duplicates=context_data.get("allow_duplicates", True),
                        unique_keys=context_data.get("unique_keys"),
                        auto_counting=context_data.get("auto_counting"),
                    )
                    created_contexts.append(name)

                except Exception as e:
                    # If creation fails, add to errors
                    errors.append(
                        {
                            "index": idx,
                            "name": name,
                            "error": str(e),
                        },
                    )
                    # Rollback the transaction to maintain consistency
                    self.session.rollback()
                    # Re-add successfully created contexts in this transaction
                    for created_name in created_contexts:
                        try:
                            # Check if it still exists (wasn't rolled back)
                            existing = self.filter(
                                project_id=project_id,
                                name=created_name,
                            )
                            if not existing:
                                # Re-create it
                                matching_context = next(
                                    (
                                        c
                                        for c in contexts
                                        if c.get("name", "").lstrip("/") == created_name
                                    ),
                                    None,
                                )
                                if matching_context:
                                    self.create(
                                        project_id=project_id,
                                        name=created_name,
                                        description=matching_context.get("description"),
                                        is_versioned=matching_context.get(
                                            "is_versioned",
                                            False,
                                        ),
                                        allow_duplicates=matching_context.get(
                                            "allow_duplicates",
                                            True,
                                        ),
                                        unique_keys=matching_context.get("unique_keys"),
                                        auto_counting=matching_context.get(
                                            "auto_counting",
                                        ),
                                    )
                        except:
                            # If re-creation fails, remove from created list
                            created_contexts.remove(created_name)

        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to bulk create contexts: {str(e)}")

        return {
            "created": created_contexts,
            "errors": errors,
        }

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
            logs_to_delete_query = (
                self.session.query(Log)
                .join(
                    LogEventLog,
                    LogEventLog.log_id == Log.id,
                )
                .filter(
                    LogEventLog.log_event_id.in_(select(log_events_subquery.c.id)),
                )
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
        unique_keys: Optional[Dict[str, str]] = None,
        auto_counting: Optional[Dict[str, Optional[str]]] = None,
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

            # Extract names and types from unique_keys dict
            unique_key_names = list(unique_keys.keys()) if unique_keys else []
            unique_key_types = list(unique_keys.values()) if unique_keys else []

            # Create the context
            stmt = pg_insert(Context).values(
                project_id=project_id,
                name=name,
                description=actual_description,
                created_at=ts,
                updated_at=ts,
                is_versioned=is_versioned,
                allow_duplicates=allow_duplicates,
                unique_key_names=unique_key_names,
                unique_key_types=unique_key_types,
                auto_counting=auto_counting or {},
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
                            unique_key_names=unique_key_names,
                            unique_key_types=unique_key_types,
                            auto_counting=auto_counting or {},
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
                    unique_keys=unique_keys,
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
            unique_keys = getattr(body, "unique_keys", None)
            return self.get_or_create(
                project_id=project_id,
                name=body.name,
                description=body.description,
                is_versioned=body.is_versioned,
                allow_duplicates=allow_duplicates,
                unique_keys=unique_keys,
            )
        else:
            # Create or get default context using upsert
            return self.get_or_create(
                project_id=project_id,
                name="",
                description="default context",
                is_versioned=False,
                unique_keys=None,
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
            SELECT l.key, l.value
            FROM log l
            JOIN log_event_log lel ON l.id = lel.log_id
            WHERE lel.log_event_id = :log_event_id
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
            JOIN log_event_log lel ON cle.id = lel.log_event_id
            JOIN log l ON lel.log_id = l.id
            GROUP BY cle.id
            HAVING COUNT(*) = (SELECT COUNT(*) FROM new_log_pairs)
        ),
        matching_pairs AS (
            SELECT
                pd.id,
                COUNT(*) as matching_count
            FROM potential_duplicates pd
            JOIN log_event_log lel ON pd.id = lel.log_event_id
            JOIN log l ON lel.log_id = l.id
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

    def check_for_duplicates_subset(
        self,
        context_id: int,
        log_event_id: int,
        keys_to_check: List[str],
    ) -> bool:
        """
        Check for duplicates based only on a subset of keys.

        Returns True if there exists another log_event in the same context whose
        values for keys_to_check match the updated log_event's values for those keys.
        """
        if not keys_to_check:
            return False

        query = """
        WITH updated_pairs AS (
            SELECT l.key, l.value
            FROM log l
            JOIN log_event_log lel ON l.id = lel.log_id
            WHERE lel.log_event_id = :log_event_id AND l.key = ANY(:keys)
        ),
        context_other_events AS (
            SELECT le.id
            FROM log_event le
            JOIN log_event_context lec ON le.id = lec.log_event_id
            WHERE lec.context_id = :context_id AND le.id != :log_event_id
        ),
        matching_other AS (
            SELECT cle.id, COUNT(*) AS match_count
            FROM context_other_events cle
            JOIN log_event_log lel ON cle.id = lel.log_event_id
            JOIN log l ON lel.log_id = l.id
            JOIN updated_pairs up ON up.key = l.key AND up.value = l.value
            WHERE l.key = ANY(:keys)
            GROUP BY cle.id
        )
        SELECT EXISTS (
            SELECT 1 FROM matching_other WHERE match_count = :num_keys
        ) AS has_duplicate
        """
        result = self.session.execute(
            text(query),
            {
                "context_id": context_id,
                "log_event_id": log_event_id,
                "keys": keys_to_check,
                "num_keys": len(keys_to_check),
            },
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
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(LogEventLog.log_event_id == original_log_id)
                    .all()
                )

                # Prepare bulk insert for Log entries
                new_logs = []
                for original_log in original_logs:
                    new_log = Log(
                        key=original_log.key,
                        value=original_log.value,
                        param_version=original_log.param_version,
                        inferred_type=original_log.inferred_type,
                    )
                    new_logs.append(new_log)

                # Bulk insert all new Log entries
                if new_logs:
                    self.session.bulk_save_objects(new_logs, return_defaults=True)
                    self.session.flush()  # Get IDs for new logs

                    # Create LogEventLog associations
                    for new_log in new_logs:
                        log_event_log = LogEventLog(
                            log_event_id=new_log_event.id,
                            log_id=new_log.id,
                        )
                        self.session.add(log_event_log)

                # Check for JSONLog entries (if the model exists)
                if JSONLog is not None:
                    try:
                        # Query JSONLog entries for the original log event via association
                        original_json_logs = (
                            self.session.query(JSONLog)
                            .join(
                                LogEventJSONLog,
                                LogEventJSONLog.json_log_id == JSONLog.id,
                            )
                            .filter(LogEventJSONLog.log_event_id == original_log_id)
                            .all()
                        )

                        # Prepare bulk insert for JSONLog entries
                        new_json_logs = []
                        for original_json_log in original_json_logs:
                            new_json_log = JSONLog(
                                key=original_json_log.key,
                                value=original_json_log.value,
                            )
                            new_json_logs.append(new_json_log)

                        # Bulk insert all new JSONLog entries
                        if new_json_logs:
                            self.session.bulk_save_objects(
                                new_json_logs,
                                return_defaults=True,
                            )
                            self.session.flush()  # Get IDs for new JSONLogs

                            # Create LogEventJSONLog associations
                            for new_json_log in new_json_logs:
                                log_event_json_log = LogEventJSONLog(
                                    log_event_id=new_log_event.id,
                                    json_log_id=new_json_log.id,
                                )
                                self.session.add(log_event_json_log)
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
            self.session.query(Log, LogEventLog.log_event_id)
            .join(LogEventLog, LogEventLog.log_id == Log.id)
            .join(LogEvent, LogEvent.id == LogEventLog.log_event_id)
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
                log_event_id=log_event_id,
                key=log.key,
                value=log.value,
                param_version=log.param_version,
                inferred_type=log.inferred_type,
                created_at=log.created_at,
                updated_at=log.updated_at,
            )
            for log, log_event_id in logs_to_version
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
                            key=lv.key,
                            value=lv.value,
                        ),
                    )

            # Bulk insert Log entries and get their IDs
            if new_logs:
                stmt = (
                    pg_insert(Log)
                    .values(
                        [
                            {
                                "key": log.key,
                                "value": log.value,
                                "param_version": log.param_version,
                                "inferred_type": log.inferred_type,
                                "created_at": log.created_at,
                                "updated_at": log.updated_at,
                            }
                            for log in new_logs
                        ],
                    )
                    .returning(Log.id)
                )
                result = self.session.execute(stmt)
                log_ids = [row[0] for row in result]

                # Create LogEventLog associations
                if log_ids:
                    log_event_log_values = [
                        {"log_event_id": new_log_event.id, "log_id": log_id}
                        for log_id in log_ids
                    ]
                    stmt_assoc = pg_insert(LogEventLog).values(log_event_log_values)
                    self.session.execute(stmt_assoc)

            # Bulk insert JSONLog entries and get their IDs
            if new_json_logs:
                stmt_json = (
                    pg_insert(JSONLog)
                    .values(
                        [
                            {
                                "key": json_log.key,
                                "value": json_log.value,
                            }
                            for json_log in new_json_logs
                        ],
                    )
                    .returning(JSONLog.id)
                )
                result_json = self.session.execute(stmt_json)
                json_log_ids = [row[0] for row in result_json]

                # Create LogEventJSONLog associations
                if json_log_ids:
                    log_event_json_log_values = [
                        {"log_event_id": new_log_event.id, "json_log_id": json_log_id}
                        for json_log_id in json_log_ids
                    ]
                    stmt_json_assoc = pg_insert(LogEventJSONLog).values(
                        log_event_json_log_values,
                    )
                    self.session.execute(stmt_json_assoc)
