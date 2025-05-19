import base64
import copy
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends
from sqlalchemy import alias, cast, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Context,
    FieldType,
    JSONLog,
    JSONLogHistory,
    Log,
    LogEvent,
    LogEventContext,
    LogHistory,
    ParamVersion,
)
from orchestra.services.bucket_service import BucketService


class OverwriteError(Exception):
    pass


class ImmutableFieldError(Exception):
    pass


def _is_date_string(value: str) -> bool:
    """
    Check if a string can be parsed as a date in various formats including:
    - YYYY-MM-DD (ISO 8601)
    - MM/DD/YYYY
    - DD/MM/YYYY
    - DD-MM-YYYY
    - Month DD, YYYY

    Args:
        value (str): The string to check

    Returns:
        bool: True if the string can be parsed as a date, False otherwise
    """
    try:
        if isinstance(value, str):
            # Remove quotes if present
            clean_value = value.strip("\"'")

            # Try different date formats
            for fmt in (
                "%Y-%m-%d",  # ISO 8601: 2023-01-31
                "%m/%d/%Y",  # US format: 01/31/2023
                "%d/%m/%Y",  # UK format: 31/01/2023
                "%d-%m-%Y",  # European format: 31-01-2023
                "%B %d, %Y",  # Month name: January 31, 2023
                "%b %d, %Y",  # Abbreviated month: Jan 31, 2023
            ):
                try:
                    parsed_date = datetime.strptime(clean_value, fmt).date()
                    # Ensure it's just a date (no time component)
                    if isinstance(parsed_date, date):
                        return True
                except ValueError:
                    continue

            # Check for ISO format with regex
            if re.match(r"^\d{4}-\d{2}-\d{2}$", clean_value):
                try:
                    date.fromisoformat(clean_value)
                    return True
                except ValueError:
                    pass
        return False
    except Exception:
        return False


def _is_timedelta_string(value: str) -> bool:
    """
    Check if a string represents a timedelta in ISO 8601 duration format.

    ISO 8601 duration format: P[n]Y[n]M[n]DT[n]H[n]M[n]S
    Examples:
    - P1Y2M3DT4H5M6S (1 year, 2 months, 3 days, 4 hours, 5 minutes, 6 seconds)
    - P1D (1 day)
    - PT1H (1 hour)

    Also checks for simple duration formats like:
    - HH:MM:SS
    - MM:SS
    - [n] days, [n] hours, etc.

    Args:
        value (str): The string to check

    Returns:
        bool: True if the string represents a timedelta, False otherwise
    """
    try:
        if isinstance(value, str):
            clean_value = value.strip("\"'")

            # Check ISO 8601 duration format
            iso_duration_pattern = r"^P(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$"
            if re.match(iso_duration_pattern, clean_value):
                return True

            # Check for PostgreSQL interval format: 1 day 2 hours 3 minutes 4 seconds
            pg_interval_pattern = r"^(\d+\s+(?:day|days|hour|hours|minute|minutes|second|seconds)(?:\s+|$))+$"
            if re.match(pg_interval_pattern, clean_value, re.IGNORECASE):
                return True

            # Check for simple time duration format: HH:MM:SS
            if re.match(r"^\d+:\d{2}(:\d{2})?$", clean_value):
                # Make sure it's not a valid time (which would be caught by _is_time_string)
                if not _is_time_string(clean_value):
                    return True
        return False
    except Exception:
        return False


def _is_time_string(value: str) -> bool:
    """
    Check if a string can be parsed as a time in various formats including:
    - HH:MM:SS[.ffffff]
    - HH:MM
    - H:MM AM/PM
    - HH:MM:SS AM/PM

    Args:
        value (str): The string to check

    Returns:
        bool: True if the string can be parsed as a time, False otherwise
    """
    try:
        # Try to parse the string as a time
        if isinstance(value, str):
            # Remove quotes if present
            clean_value = value.strip("\"'")
            # Try different time formats
            for fmt in (
                "%H:%M:%S",  # 24-hour with seconds: 14:30:45
                "%H:%M:%S.%f",  # 24-hour with seconds and microseconds: 14:30:45.123
                "%H:%M",  # 24-hour without seconds: 14:30
                "%I:%M %p",  # 12-hour without seconds: 2:30 PM
                "%I:%M:%S %p",  # 12-hour with seconds: 02:30:45 PM
                "%I:%M:%S.%f %p",  # 12-hour with seconds and microseconds: 02:30:45.123 PM
            ):
                try:
                    datetime.strptime(clean_value, fmt)
                    return True
                except ValueError:
                    continue
        return False
    except Exception:
        return False


def normalize_timestamp(ts_str: str) -> str:
    """
    Attempts to parse the provided timestamp string and return an ISO formatted string.

    This function tries to convert various timestamp formats to the ISO 8601 format
    with the 'T' separator, which is the standard format used in the database.
    """
    try:
        # First try direct ISO format; if it fails, try common alternative formats
        dt = datetime.fromisoformat(ts_str)
    except ValueError:
        # Try alternative formats without 'T', e.g. '%Y-%m-%d %H:%M:%S.%f' or '%Y-%m-%d %H:%M:%S'
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(ts_str, fmt)
                break
            except ValueError:
                continue
        else:
            # If no format matches, return the original string
            return ts_str
    return dt.isoformat()


# noinspection PyBroadException
class LogDAO:
    def __init__(
        self,
        session: Session = Depends(get_db_session),
        context_dao: ContextDAO = Depends(ContextDAO),
    ):
        self.session = session
        self.bucket_service = BucketService()
        self.context_dao = context_dao

    def _create_log_history(
        self,
        log_event_id: int,
        key: str,
        value: Any,
        version: int,
        inferred_type: Optional[str],
        description: str,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ) -> LogHistory:
        """Helper method to create a LogHistory entry."""
        log_history = LogHistory(
            log_event_id=log_event_id,
            key=key,
            value=value,
            version=version,
            inferred_type=inferred_type,
            description=description,
            archived_at=datetime.now(timezone.utc),
        )
        if created_at:
            log_history.created_at = created_at
        if updated_at:
            log_history.updated_at = updated_at
        self.session.add(log_history)
        return log_history

    def _create_json_log_history(
        self,
        log_event_id: int,
        key: str,
        value: Any,
        version: int,
        description: str,
    ) -> JSONLogHistory:
        """Helper method to create a JSONLogHistory entry."""
        json_log_history = JSONLogHistory(
            log_event_id=log_event_id,
            key=key,
            value=value,
            version=version,
            description=description,
            archived_at=datetime.now(timezone.utc),
        )
        self.session.add(json_log_history)
        return json_log_history

    def _handle_versioned_history(
        self,
        context_id: Optional[int],
        log_event_id: int,
        key: str,
        value: Any,
        inferred_type: Optional[str] = None,
        description: str = "",
        json_value: Any = None,
    ) -> Optional[Context]:
        """Helper method to handle versioned history creation for both Log and JSONLog entries."""
        if context_id is None:
            return None

        context = self.session.query(Context).filter_by(id=context_id).first()
        if not context or not context.is_versioned:
            return None

        # Create regular log history
        self._create_log_history(
            log_event_id=log_event_id,
            key=key,
            value=value,
            version=context.version,
            inferred_type=inferred_type,
            description=description,
        )

        # Create JSON log history if json_value is provided
        if json_value is not None:
            self._create_json_log_history(
                log_event_id=log_event_id,
                key=key,
                value=json_value,
                version=context.version,
                description=description,
            )

        context.updated_at = datetime.now(timezone.utc)
        return context

    def upload_image_to_bucket(self, image_base64: str) -> str:
        """Upload image to bucket and return the URL."""
        try:
            url, _ = self.bucket_service.upload_image(image_base64)
            return url
        except Exception as e:
            raise ValueError(f"Failed to upload image to bucket: {str(e)}")

    def get_image_from_bucket(self, url: str) -> Optional[str]:
        """Retrieve image from bucket and return as base64."""
        try:
            # Extract filename from URL
            filename = url.split("/")[-1]
            base64_content = self.bucket_service.get_image(filename)
            return base64_content
        except Exception as e:
            raise ValueError(f"Failed to retrieve image from bucket: {str(e)}")

    @staticmethod
    def possible_img(raw_k):
        lower = raw_k.lower()
        return (
            "img" in lower
            or "image" in lower
            or "photo" in lower
            or "diagram" in lower
            or "pic" in lower
        )

    @staticmethod
    def infer_type(raw_k, raw_v):
        maybe_img = LogDAO.possible_img(raw_k)
        if isinstance(raw_v, str):
            try:
                if _is_time_string(raw_v):
                    return "time"
                if _is_date_string(raw_v):
                    return "date"
                if _is_timedelta_string(raw_v):
                    return "timedelta"

                datetime.fromisoformat(raw_v)
                return "timestamp"
            except:
                if not maybe_img:
                    return "str"
                binary = raw_v.encode("utf-8")
                try:
                    assert base64.b64encode(base64.b64decode(binary)) == binary
                    return "image"
                except:
                    lower = raw_v.lower()
                    if lower.startswith("http") and (
                        lower.endswith(".png")
                        or lower.endswith(".jpg")
                        or lower.endswith(".jpeg")
                    ):
                        return "image"
                    return "str"
        return type(raw_v).__name__

    def get_ids_by_filter(
        self,
        project_id: int,
        filters: Dict[str, Any],
        context_ids: Optional[List[int]] = None,
    ) -> List[int]:
        """
        Get log_event_ids that match the given filters for a project.

        Args:
            project_id: The project ID to filter by
            filters: Dictionary of key-value pairs to filter logs by
            context_ids: Optional list of context IDs to further filter the logs

        Returns:
            List of log_event_ids that match the filters
        """
        if not filters:
            return []

        # Start with a query for log events in the project
        query = select(LogEvent.id).where(LogEvent.project_id == project_id)

        # If context_ids are provided, filter by those contexts
        if context_ids:
            query = query.join(
                LogEventContext,
                LogEventContext.log_event_id == LogEvent.id,
            ).where(LogEventContext.context_id.in_(context_ids))

        # For each key-value pair in filters, add a join to Log and filter condition
        for idx, (key, value) in enumerate(filters.items()):
            # Create a unique alias for each Log join to avoid conflicts
            log_alias = f"log_{idx}"
            log_table = alias(Log, name=log_alias)

            # Join with the Log table
            query = query.join(
                log_table,
                log_table.c.log_event_id == LogEvent.id,
            )

            # Add filter conditions for this key-value pair
            query = query.where(
                log_table.c.key == key,
                log_table.c.value == literal(value, type_=JSONB),
            )

        # Execute the query and return the list of log_event_ids
        result = self.session.execute(query)
        return [row[0] for row in result]

    def filter(
        self,
        id: Optional[Union[int, List[int]]] = None,
        log_event_id: Optional[Union[int, List[int]]] = None,
        key: Optional[Union[str, List[str]]] = None,
        value: Optional[Union[str, List[str]]] = None,
        version: Optional[Union[int, List[int]]] = None,
        inferred_type: Optional[Union[str, List[str]]] = None,
        project_id: Optional[int] = None,
        defer: bool = False,
    ) -> List[Log]:
        def normalize_input(value):
            if value is None or isinstance(value, list):
                return value
            return [value]

        id = normalize_input(id)
        log_event_id = normalize_input(log_event_id)
        key = normalize_input(key)
        value = normalize_input(value)
        version = normalize_input(version)
        inferred_type = normalize_input(inferred_type)

        if (
            id == []
            or log_event_id == []
            or key == []
            or value == []
            or version == []
            or inferred_type == []
        ):
            return []

        query = select(Log, LogEvent.created_at.label("log_event_ts")).join(
            LogEvent,
            LogEvent.id == Log.log_event_id,
        )
        if id:
            query = query.where(Log.id.in_(id))
        if log_event_id:
            query = query.where(Log.log_event_id.in_(log_event_id))
        if key:
            query = query.where(Log.key.in_(key))
        if value:
            cast_values = [cast(literal(val), JSONB) for val in value]
            query = query.where(Log.value.in_(cast_values))
        if version:
            query = query.where(Log.version.in_(version))
        if inferred_type:
            query = query.where(Log.inferred_type.in_(inferred_type))
        if project_id:
            query = query.where(LogEvent.project_id == project_id)

        query = query.order_by(Log.created_at)
        rows = self.session.execute(query)
        if defer:
            return rows
        return rows.fetchall()

    def rename_field_in_logs(
        self,
        project_id: int,
        old_field_name: str,
        new_field_name: str,
        context_id: Optional[int] = None,
    ) -> None:
        """
        Rename a field across all log tables while maintaining data consistency.

        Args:
            project_id: The project ID to scope the rename operation
            old_field_name: The current field name to be renamed
            new_field_name: The new field name
            context_id: Optional context ID to scope the rename operation

        Raises:
            ValueError: If the field names are invalid or if the rename operation fails
        """
        try:
            # Start by finding all relevant log events for the project
            log_event_query = select(LogEvent.id).where(
                LogEvent.project_id == project_id,
            )
            if context_id:
                log_event_query = log_event_query.join(
                    LogEventContext,
                    LogEventContext.log_event_id == LogEvent.id,
                ).where(LogEventContext.context_id == context_id)

            log_event_ids = [row[0] for row in self.session.execute(log_event_query)]

            if not log_event_ids:
                raise ValueError(f"No log events found for project_id {project_id}")

            # Update Log table
            log_update = (
                self.session.query(Log)
                .filter(
                    Log.log_event_id.in_(log_event_ids),
                    Log.key == old_field_name,
                )
                .update(
                    {"key": new_field_name, "updated_at": datetime.now(timezone.utc)},
                    synchronize_session=False,
                )
            )

            # Update JSONLog table
            json_log_update = (
                self.session.query(JSONLog)
                .filter(
                    JSONLog.log_event_id.in_(log_event_ids),
                    JSONLog.key == old_field_name,
                )
                .update({"key": new_field_name}, synchronize_session=False)
            )

            # Update LogHistory table
            log_history_update = (
                self.session.query(LogHistory)
                .filter(
                    LogHistory.log_event_id.in_(log_event_ids),
                    LogHistory.key == old_field_name,
                )
                .update({"key": new_field_name}, synchronize_session=False)
            )

            # Update JSONLogHistory table
            json_log_history_update = (
                self.session.query(JSONLogHistory)
                .filter(
                    JSONLogHistory.log_event_id.in_(log_event_ids),
                    JSONLogHistory.key == old_field_name,
                )
                .update({"key": new_field_name}, synchronize_session=False)
            )

            # If this is a versioned context, create history entries for the rename
            if context_id:
                context = self.session.query(Context).filter_by(id=context_id).first()
                if context and context.is_versioned:
                    # Get all affected logs to create history entries
                    affected_logs = (
                        self.session.query(Log)
                        .filter(
                            Log.log_event_id.in_(log_event_ids),
                            Log.key == new_field_name,
                        )
                        .all()
                    )

                    for log in affected_logs:
                        self._create_log_history(
                            log_event_id=log.log_event_id,
                            key=new_field_name,
                            value=log.value,
                            version=context.version,
                            inferred_type=log.inferred_type,
                            description=f"Renamed field from {old_field_name} to {new_field_name}",
                            created_at=log.created_at,
                            updated_at=log.updated_at,
                        )

            self.session.commit()

        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to rename field: {str(e)}")

    def delete(self, id: int):
        try:
            # First get the log and check if it belongs to a versioned context
            log = self.session.query(Log).filter_by(id=id).one()

            # Check if this log is part of a versioned context
            log_event_context = (
                self.session.query(LogEventContext)
                .filter_by(
                    log_event_id=log.log_event_id,
                )
                .first()
            )

            if log_event_context:
                # Handle versioned history
                self._handle_versioned_history(
                    context_id=log_event_context.context_id,
                    log_event_id=log.log_event_id,
                    key=log.key,
                    value=log.value,
                    inferred_type=log.inferred_type,
                    description=f"Deleted entry with key {log.key}",
                )

            # Proceed with log deletion
            json_log = (
                self.session.query(JSONLog)
                .filter_by(log_event_id=log.log_event_id, key=log.key)
                .first()
            )
            if json_log:
                self.session.delete(json_log)
            self.session.delete(log)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    def bulk_create(
        self,
        entries: List[Dict[str, Any]],
    ) -> List[int]:
        """
        Create multiple Log entries in a single database transaction.

        Args:
            entries: List of dictionaries with the following keys:
                - project_id: int
                - log_event_id: int
                - key: str
                - value: Any (optional)
                - version: int (optional)
                - explicit_types: Dict (optional)
                - context_id: int (optional)

        Returns:
            List of created log IDs
        """
        if not entries:
            return []

        # Start transaction
        # created_ids = []
        logs_to_create = []
        json_logs_to_create = []
        history_entries = []
        json_history_entries = []
        contexts_to_update = set()

        try:
            now = datetime.now(timezone.utc)

            # Process each entry
            for entry in entries:
                project_id = entry.get("project_id")
                log_event_id = entry.get("log_event_id")
                key = entry.get("key")
                value = entry.get("value")
                version = entry.get("version")
                explicit_types = entry.get("explicit_types", {})
                key_explicit_type = explicit_types.get(key, {})
                inferred_type = key_explicit_type.get("type")
                context_id = entry.get("context_id")

                if not all([log_event_id, key]):
                    continue

                # Handle enum type
                if inferred_type == "enum" and project_id is not None:
                    # Extract enum values and restrict flag
                    enum_values = key_explicit_type.get("values")
                    enum_restrict = key_explicit_type.get("restrict", False)

                    # Handle enum field type
                    try:
                        self._handle_enum_field_type(
                            project_id=project_id,
                            context_id=context_id,
                            key=key,
                            value=value,
                            enum_values=enum_values,
                            enum_restrict=enum_restrict,
                        )
                        # If enum field type is created, infer type as str
                        inferred_type = "str"
                    except ValueError as e:
                        raise e
                elif inferred_type is None:
                    inferred_type = self.infer_type(key, value)

                # Handle image uploads
                if (
                    inferred_type == "image"
                    and isinstance(value, str)
                    and not value.lower().startswith("http")
                ):
                    value = self.upload_image_to_bucket(value)
                if inferred_type == "timestamp" and isinstance(value, str):
                    value = normalize_timestamp(value)
                # Handle versioned history
                if context_id is not None:
                    context = (
                        self.session.query(Context).filter_by(id=context_id).first()
                    )
                    if context and context.is_versioned:
                        # Create history entry
                        history_entries.append(
                            {
                                "log_event_id": log_event_id,
                                "key": key,
                                "value": value,
                                "version": context.version,
                                "inferred_type": inferred_type,
                                "description": f"Created entry with key {key}",
                                "archived_at": now,
                            },
                        )

                        # If JSON, also create JSON history
                        if isinstance(value, (dict, list)):
                            json_history_entries.append(
                                {
                                    "log_event_id": log_event_id,
                                    "key": key,
                                    "value": value,
                                    "version": context.version,
                                    "description": f"Created entry with key {key}",
                                    "archived_at": now,
                                },
                            )

                        # Mark context for update
                        contexts_to_update.add(context.id)

                # Create JSON log for dict/list values
                if isinstance(value, (dict, list)):
                    json_logs_to_create.append(
                        JSONLog(
                            log_event_id=log_event_id,
                            key=key,
                            value=value,
                        ),
                    )

                # Create Log entry
                if version is not None:
                    # For versioned logs, use upsert to handle concurrency
                    insert_stmt = (
                        pg_insert(Log)
                        .values(
                            log_event_id=log_event_id,
                            key=key,
                            version=version,
                            value=value,
                            inferred_type=inferred_type,
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["log_event_id", "key", "version"],
                        )
                    )
                    result = self.session.execute(insert_stmt)

                    # Check if inserted or if conflict existed
                    if result.rowcount == 1:
                        # Get the ID of the new row
                        new_log = (
                            self.session.query(Log)
                            .filter_by(
                                log_event_id=log_event_id,
                                key=key,
                                version=version,
                            )
                            .one()
                        )
                        # created_ids.append(new_log.id)
                    else:
                        # Check if existing row has the same value
                        existing_log = (
                            self.session.query(Log)
                            .filter_by(
                                log_event_id=log_event_id,
                                key=key,
                                version=version,
                            )
                            .one()
                        )
                        if existing_log.value != value:
                            raise ValueError(
                                f"Version mismatch: Attempted to insert (log_event_id={log_event_id}, "
                                f"key='{key}', version={version}) with a different value.\n"
                                f"Existing: {existing_log.value}\nNew: {value}",
                            )
                        # created_ids.append(existing_log.id)
                else:
                    # For non-versioned logs, add to bulk create list
                    log = Log(
                        log_event_id=log_event_id,
                        key=key,
                        value=value,
                        version=None,
                        inferred_type=inferred_type,
                        created_at=now,
                        updated_at=now,
                    )
                    logs_to_create.append(log)

            # Bulk save non-versioned logs
            if logs_to_create:
                self.session.bulk_save_objects(logs_to_create)
                self.session.flush()

            # Bulk save JSON logs
            if json_logs_to_create:
                self.session.bulk_save_objects(json_logs_to_create)

            # Create history entries for versioned contexts
            for entry in history_entries:
                log_history = LogHistory(**entry)
                self.session.add(log_history)

            for entry in json_history_entries:
                json_log_history = JSONLogHistory(**entry)
                self.session.add(json_log_history)

            # Update timestamps on contexts
            for context_id in contexts_to_update:
                context = self.session.query(Context).filter_by(id=context_id).first()
                if context:
                    context.updated_at = now

            self.session.commit()
            # return created_ids

        except Exception as e:
            raise e

    def get_next_param_version(
        self,
        project_id: int,
        context_id: int,
        param_key: str,
    ) -> int:
        """
        Atomically obtain and increment the version counter for a given parameter key.

        Args:
            project_id: The project identifier.
            param_key: The parameter key.

        Returns:
            The next version number for the parameter.
        """
        # Attempt to insert a new row with initial version -1 if not exists
        insert_stmt = (
            pg_insert(ParamVersion)
            .values(
                project_id=project_id,
                context_id=context_id,
                param_key=param_key,
                last_version=-1,
            )
            .on_conflict_do_nothing(
                index_elements=["project_id", "context_id", "param_key"],
            )
        )
        self.session.execute(insert_stmt)

        # Atomically update the row and get new version number
        result = self.session.execute(
            text(
                """
                UPDATE param_version
                SET last_version = last_version + 1
                WHERE project_id = :project_id AND context_id = :context_id AND param_key = :param_key
                RETURNING last_version
            """,
            ),
            {
                "project_id": project_id,
                "context_id": context_id,
                "param_key": param_key,
            },
        )
        row = result.fetchone()
        if row is None:
            raise ValueError(f"Failed to get next version for parameter {param_key}")
        return row[0]

    def _upsert_json_log(
        self,
        log_event_id: int,
        key: str,
        value: Any,
        overwrite: bool,
    ) -> int:
        """
        Upsert a JSONLog entry using PostgreSQL's INSERT ... ON CONFLICT.

        Args:
            log_event_id: The log event ID
            key: The key for the JSON log
            value: The JSON value to store
            overwrite: Whether to update existing entries (True) or do nothing (False)

        Returns:
            int: The number of rows affected (1 for insert/update, 0 for no change)
        """
        stmt = pg_insert(JSONLog).values(
            log_event_id=log_event_id,
            key=key,
            value=value,
        )
        if overwrite:
            stmt = stmt.on_conflict_do_update(
                index_elements=["log_event_id", "key"],
                set_={"value": stmt.excluded.value},
            )
        else:
            stmt = stmt.on_conflict_do_nothing()

        result = self.session.execute(stmt)
        return result.rowcount

    def _upsert_log(
        self,
        log_event_id: int,
        key: str,
        value: Any,
        inferred_type: str,
        version: Optional[int] = None,
        overwrite: bool = False,
    ) -> int:
        """
        Upsert a Log entry using PostgreSQL's INSERT ... ON CONFLICT.

        Args:
            log_event_id: The log event ID
            key: The key for the log
            value: The value to store
            inferred_type: The inferred type of the value
            version: Optional version number
            overwrite: Whether to update existing entries (True) or do nothing (False)

        Returns:
            int: The number of rows affected (1 for insert/update, 0 for no change)
        """
        stmt = pg_insert(Log).values(
            log_event_id=log_event_id,
            key=key,
            value=value,
            version=version,
            inferred_type=inferred_type,
            created_at=func.now(),
            updated_at=func.now(),
        )
        if overwrite:
            stmt = stmt.on_conflict_do_update(
                index_elements=["log_event_id", "key", "version"],
                set_={
                    "value": stmt.excluded.value,
                    "updated_at": func.now(),
                },
            )
        else:
            stmt = stmt.on_conflict_do_nothing()

        result = self.session.execute(stmt)
        return result.rowcount

    def bulk_update(
        self,
        updates: List[Dict[str, Any]],
        overwrite: bool = False,
        field_types: Optional[Dict] = None,
    ) -> None:
        """
        Update multiple Log entries in a single database transaction.

        Args:
            updates: List of dictionaries with the following keys:
                - log_event_id: int
                - key: str
                - value: Any
                - version: int (optional)
                - explicit_types: Dict (optional)
                - context_id: int (optional)
            overwrite: Whether to allow overwriting existing values
            field_types: Dictionary of field types with mutable flags

        Raises:
            OverwriteError: If overwrite=False and a value already exists
            ImmutableFieldError: If a field is marked as immutable in field_types
            ValueError: If any other error occurs during update
        """
        if not updates:
            return

        field_types = field_types or {}

        try:
            now = datetime.now(timezone.utc)

            # Group updates by log_event_id and key for efficient querying
            update_groups = {}
            for update in updates:
                log_event_id = update.get("log_event_id")
                key = update.get("key")
                if not log_event_id or not key:
                    continue

                group_key = (log_event_id, key)
                update_groups[group_key] = update

            if not update_groups:
                return

            # Query all existing logs in one go
            log_event_ids = [k[0] for k in update_groups.keys()]
            keys = [k[1] for k in update_groups.keys()]
            existing_logs = (
                self.session.query(Log)
                .filter(Log.log_event_id.in_(log_event_ids))
                .filter(Log.key.in_(keys))
                .all()
            )

            # Create a lookup for existing logs
            existing_log_map = {
                (log.log_event_id, log.key): log for log in existing_logs
            }

            # Query all existing JSON logs in one go
            existing_json_logs = (
                self.session.query(JSONLog)
                .filter(JSONLog.log_event_id.in_(log_event_ids))
                .filter(JSONLog.key.in_(keys))
                .all()
            )

            # Create a lookup for existing JSON logs
            existing_json_log_map = {
                (json_log.log_event_id, json_log.key): json_log
                for json_log in existing_json_logs
            }

            # Process all context IDs at once
            context_ids = [
                update.get("context_id")
                for update in update_groups.values()
                if update.get("context_id") is not None
            ]
            context_map = {}
            if context_ids:
                contexts = (
                    self.session.query(Context)
                    .filter(Context.id.in_(context_ids))
                    .all()
                )
                context_map = {context.id: context for context in contexts}

            # Collect history entries to create and JSON logs to create/update
            history_entries = []
            json_history_entries = []
            json_logs_to_create = []
            contexts_to_update = set()
            log_event_ids_to_update = set()

            # Process each update
            for group_key, update in update_groups.items():
                log_event_id, key = group_key
                value = update.get("value")
                version = update.get("version")
                explicit_types = update.get("explicit_types", {})
                key_explicit_type = explicit_types.get(key, {})
                inferred_type = key_explicit_type.get("type")
                context_id = update.get("context_id")

                # Get project_id from log_event
                log_event = (
                    self.session.query(LogEvent).filter_by(id=log_event_id).first()
                )
                project_id = log_event.project_id if log_event else None

                # Handle enum type
                if inferred_type == "enum" and project_id is not None:
                    # Extract enum values and restrict flag
                    enum_values = key_explicit_type.get("values")
                    enum_restrict = key_explicit_type.get("restrict", False)

                    # Handle enum field type
                    try:
                        self._handle_enum_field_type(
                            project_id=project_id,
                            context_id=context_id,
                            key=key,
                            value=value,
                            enum_values=enum_values,
                            enum_restrict=enum_restrict,
                        )
                        # If enum field type is created, infer type as str
                        inferred_type = "str"
                    except ValueError as e:
                        raise e
                elif inferred_type is None:
                    inferred_type = self.infer_type(key, value)

                # Handle image uploads
                json_value = value
                if (
                    inferred_type == "image"
                    and isinstance(value, str)
                    and not value.lower().startswith("http")
                ):
                    json_value = self.upload_image_to_bucket(value)

                # Check if log exists
                existing_log = existing_log_map.get(group_key)

                # Check for context versioning
                context = (
                    context_map.get(context_id) if context_id is not None else None
                )
                is_versioned = context and context.is_versioned

                if existing_log:
                    # Check if overwrite is allowed
                    if not update.get("overwrite", False):
                        raise OverwriteError

                    # Check if field is immutable
                    if key in field_types and context_id is not None:
                        if not is_versioned:
                            field_info = field_types.get(key)
                            if field_info and not field_info.get("mutable", False):
                                raise ImmutableFieldError

                    # Update existing log
                    existing_log.value = json_value
                    existing_log.version = version
                    existing_log.inferred_type = inferred_type
                    existing_log.updated_at = now

                    # Handle versioned history
                    if is_versioned:
                        # Create history entry for current value before updating
                        history_entries.append(
                            {
                                "log_event_id": log_event_id,
                                "key": key,
                                "value": existing_log.value,
                                "version": context.version,
                                "inferred_type": existing_log.inferred_type,
                                "description": f"Updated entry with key {key}",
                                "archived_at": now,
                            },
                        )
                        contexts_to_update.add(context_id)
                else:
                    # Entry doesn't exist, create new log
                    new_log = Log(
                        log_event_id=log_event_id,
                        key=key,
                        value=json_value,
                        version=version,
                        inferred_type=inferred_type,
                        created_at=now,
                        updated_at=now,
                    )
                    self.session.add(new_log)

                    # Handle versioned history for new logs
                    if is_versioned:
                        history_entries.append(
                            {
                                "log_event_id": log_event_id,
                                "key": key,
                                "value": json_value,
                                "version": context.version,
                                "inferred_type": inferred_type,
                                "description": f"Created entry with key {key}",
                                "archived_at": now,
                            },
                        )
                        contexts_to_update.add(context_id)

                # Handle JSON logs for dict/list values
                if isinstance(value, (dict, list)):
                    existing_json_log = existing_json_log_map.get(group_key)

                    if existing_json_log:
                        # Update existing JSON log
                        existing_json_log.value = value

                        # Create JSON history if versioned
                        if is_versioned:
                            json_history_entries.append(
                                {
                                    "log_event_id": log_event_id,
                                    "key": key,
                                    "value": existing_json_log.value,
                                    "version": context.version,
                                    "description": f"Updated JSON entry with key {key}",
                                    "archived_at": now,
                                },
                            )

                    else:
                        # Create new JSON log
                        new_json_log = JSONLog(
                            log_event_id=log_event_id,
                            key=key,
                            value=value,
                        )
                        json_logs_to_create.append(new_json_log)

                        # Create JSON history if versioned
                        if is_versioned:
                            json_history_entries.append(
                                {
                                    "log_event_id": log_event_id,
                                    "key": key,
                                    "value": value,
                                    "version": context.version,
                                    "description": f"Created JSON entry with key {key}",
                                    "archived_at": now,
                                },
                            )

                # Track log events to update timestamps
                log_event_ids_to_update.add(log_event_id)

            # Create history entries
            for entry in history_entries:
                log_history = LogHistory(**entry)
                self.session.add(log_history)

            for entry in json_history_entries:
                json_log_history = JSONLogHistory(**entry)
                self.session.add(json_log_history)

            # Bulk save JSON logs
            if json_logs_to_create:
                self.session.bulk_save_objects(json_logs_to_create)

            # Update context timestamps
            for context_id in contexts_to_update:
                context = context_map.get(context_id)
                if context:
                    context.updated_at = now

            # Update log event timestamps
            for log_event_id in log_event_ids_to_update:
                log_event = (
                    self.session.query(LogEvent).filter_by(id=log_event_id).first()
                )
                if log_event:
                    log_event.updated_at = now

            self.session.commit()

        except (OverwriteError, ImmutableFieldError):
            raise
        except Exception as e:
            raise ValueError(f"Failed to perform bulk update: {str(e)}")

    def _handle_enum_field_type(
        self,
        project_id: int,
        context_id: Optional[int],
        key: str,
        value: Any,
        enum_values: Optional[List[str]],
        enum_restrict: bool,
    ) -> None:
        """
        Handle enum field type creation, update, and validation.

        Args:
            project_id: The project ID
            context_id: Optional context ID
            key: The field name
            value: The field value to validate
            enum_values: List of allowed enum values
            enum_restrict: Whether to restrict to allowed values

        Raises:
            ValueError: If value is not in allowed enum values when restrict=True
        """
        # Query for existing field type
        field_type = (
            self.session.query(FieldType)
            .filter_by(
                project_id=project_id,
                field_name=key,
                context_id=context_id,
            )
            .first()
        )

        if field_type:
            # Validate or update enum values
            if isinstance(value, str):
                current_enum_values = field_type.enum_values
                if value not in current_enum_values:
                    if field_type.enum_restrict:
                        # Only enforce restriction if explicit non-empty values were provided
                        raise ValueError(
                            f"Value '{value}' is not in allowed enum values for field '{key}': {current_enum_values}",
                        )
                    else:
                        # Auto-expand enum values for open enums using FieldTypeDAO
                        # Use SQLAlchemy's update with array_append to atomically append the value
                        new_values = (
                            list(set([value] + enum_values))
                            if isinstance(enum_values, list)
                            else [value]
                        )
                        stmt = (
                            update(FieldType)
                            .where(
                                FieldType.project_id == project_id,
                                FieldType.field_name == key,
                                FieldType.context_id == context_id,
                            )
                            .values(
                                # Use PostgreSQL's array_append function to add the value
                                # This is done at the database level to avoid race conditions
                                enum_values=FieldType.enum_values.concat(new_values),
                            )
                        )
                        self.session.execute(stmt)

            if enum_restrict:
                field_type.enum_restrict = enum_restrict

        else:
            # Create new field type
            field_type = FieldType(
                project_id=project_id,
                context_id=context_id,
                field_name=key,
                field_type="enum",
                field_category="entry",
                enum_values=[] if enum_values is None else enum_values,
                enum_restrict=enum_restrict,
            )
            self.session.add(field_type)

    def _apply_patch_to_doc(self, doc, segments, new_value, overwrite=False):
        """
        Apply a patch to a nested location in a JSON document.

        Args:
            doc: The JSON document (dict or list) to modify
            segments: List of path segments (strings or numeric strings)
            new_value: The value to set at the target location
            overwrite: Whether to allow overwriting existing values

        Returns:
            The modified document

        Raises:
            OverwriteError: If overwrite=False and the existing value differs from new_value
            ValueError: If the path is invalid or segments don't match document structure
        """
        if not segments:
            return new_value

        # Make a deep copy to avoid modifying the original during navigation
        current = doc
        parent = None
        final_key = segments[-1]

        # Navigate to the parent of the target location
        for i, segment in enumerate(segments[:-1]):
            parent = current

            if isinstance(current, dict):
                if segment not in current:
                    current[segment] = {} if i < len(segments) - 2 else {}
                current = current[segment]
            elif isinstance(current, list):
                try:
                    idx = int(segment)
                    if idx < 0 or idx >= len(current):
                        raise ValueError(
                            f"List index {idx} out of range for segment {segment}",
                        )
                    current = current[idx]
                except ValueError:
                    raise ValueError(f"Invalid list index: {segment}")
            else:
                raise ValueError(
                    f"Cannot navigate through non-container at segment {segment}",
                )

        # Handle the final segment
        if isinstance(current, dict):
            # Check if we're allowed to overwrite
            if (
                not overwrite
                and final_key in current
                and current[final_key] != new_value
            ):
                raise OverwriteError(f"Cannot overwrite existing value at {segments}")
            current[final_key] = new_value
        elif isinstance(current, list):
            try:
                idx = int(final_key)
                if idx < 0 or idx >= len(current):
                    raise ValueError(
                        f"List index {idx} out of range for final segment {final_key}",
                    )
                # Check if we're allowed to overwrite
                if not overwrite and current[idx] != new_value:
                    raise OverwriteError(
                        f"Cannot overwrite existing value at {segments}",
                    )
                current[idx] = new_value
            except ValueError:
                raise ValueError(f"Invalid list index for final segment: {final_key}")
        else:
            raise ValueError(f"Cannot set path in scalar at final segment {final_key}")

        return doc

    def apply_jsonb_patch(
        self,
        patches: List[Dict[str, Any]],
        overwrite: bool = False,
        field_types: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Apply JSONB patches to nested paths within Log.value and JSONLog.value.
        This allows partial updates to nested JSON objects in a single database transaction.
        """
        if not patches:
            return

        field_types = field_types or {}

        try:
            now = datetime.now(timezone.utc)
            # Group patches by (log_event_id, base_key)
            grouped = {}
            for patch in patches:
                le_id = patch.get("log_event_id")
                base_key = patch.get("base_key")
                if not le_id or not base_key:
                    continue
                grouped.setdefault((le_id, base_key), []).append(patch)

            for (le_id, base_key), group in grouped.items():
                # Lock the Log row for update
                log_entry = (
                    self.session.query(Log)
                    .filter_by(log_event_id=le_id, key=base_key)
                    .first()
                )
                if not log_entry:
                    raise ValueError(
                        f"Log entry not found for log_event_id={le_id}, key='{base_key}'",
                    )

                # Check mutability
                ft_info = field_types.get(base_key)
                if ft_info and not ft_info.get("mutable", False):
                    raise ImmutableFieldError(f"Field '{base_key}' is immutable")

                # Determine versioned context
                context_id = group[0].get("context_id")
                context = None
                is_versioned = False
                if context_id is not None:
                    context = (
                        self.session.query(Context).filter_by(id=context_id).first()
                    )
                    is_versioned = bool(context and context.is_versioned)

                # Get the corresponding JSONLog if it exists
                json_log = (
                    self.session.query(JSONLog)
                    .filter_by(log_event_id=le_id, key=base_key)
                    .first()
                )

                # Get current document value
                current_doc = copy.deepcopy(log_entry.value)

                # Handle versioned history before any modifications if needed
                if is_versioned:
                    self._handle_versioned_history(
                        context_id=context_id,
                        log_event_id=le_id,
                        key=base_key,
                        value=current_doc,
                        inferred_type=log_entry.inferred_type,
                        description=f"Patched nested JSON document",
                        json_value=current_doc,
                    )

                for patch in group:
                    path_str = patch.get("path_segments", "")
                    new_value = patch.get("new_value")
                    patch_overwrite = patch.get("overwrite", overwrite)

                    # Parse path_segments into list of keys and indices
                    segments = []
                    s = path_str
                    i = 0
                    while i < len(s):
                        if s[i] == ".":
                            j = i + 1
                            token = ""
                            while j < len(s) and s[j] not in ".[":
                                token += s[j]
                                j += 1
                            segments.append(token)
                            i = j
                        elif s[i] == "[":
                            j = i + 1
                            token = ""
                            while j < len(s) and s[j] != "]":
                                token += s[j]
                                j += 1
                            segments.append(token)
                            i = j + 1
                        else:
                            j = i
                            token = ""
                            while j < len(s) and s[j] not in ".[":
                                token += s[j]
                                j += 1
                            segments.append(token)
                            i = j

                    # Apply the patch to the document
                    try:
                        current_doc = self._apply_patch_to_doc(
                            current_doc,
                            segments,
                            new_value,
                            patch_overwrite,
                        )
                    except Exception as e:
                        raise e

                # Update the Log entry with the modified document
                log_entry.value = current_doc
                log_entry.updated_at = now

                # Update the JSONLog entry if it exists
                if json_log:
                    json_log.value = current_doc

            self.session.commit()

        except (OverwriteError, ImmutableFieldError):
            self.session.rollback()
            raise
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to apply JSONB patch: {str(e)}")
