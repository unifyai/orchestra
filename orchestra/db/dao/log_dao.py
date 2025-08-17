import base64
import copy
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import alias, and_, cast, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.query import Query

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.models.orchestra_models import (
    Context,
    FieldType,
    JSONLog,
    Log,
    LogEvent,
    LogEventContext,
    LogEventJSONLog,
    LogEventLog,
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
            iso_duration_pattern = r"^P(?=\d|T\d)(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$"
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
        session: Session,
        context_dao: ContextDAO,
    ):
        self.session = session
        self.bucket_service = BucketService()
        self.context_dao = context_dao

    def upload_image_to_bucket(self, image_base64: str) -> str:
        """Upload image to bucket and return the URL."""
        try:
            url, _ = self.bucket_service.upload_media(image_base64, "image/jpeg")
            return url
        except Exception as e:
            raise ValueError(f"Failed to upload image to bucket: {str(e)}")

    def upload_audio_to_bucket(self, audio_base64: str) -> str:
        """Upload audio to bucket and return the URL."""
        try:
            url, _ = self.bucket_service.upload_media(audio_base64, "audio/mpeg")
            return url
        except Exception as e:
            raise ValueError(f"Failed to upload audio to bucket: {str(e)}")

    def get_image_from_bucket(self, url: str) -> Optional[str]:
        """Retrieve image from bucket and return as base64."""
        try:
            # Extract filename from URL
            filename = url.split("/")[-1]
            base64_content = self.bucket_service.get_media(filename)
            return base64_content
        except Exception as e:
            raise ValueError(f"Failed to retrieve image from bucket: {str(e)}")

    def get_audio_from_bucket(self, url: str) -> Optional[str]:
        """Retrieve audio from bucket and return as base64."""
        try:
            # Extract filename from URL
            filename = url.split("/")[-1]
            base64_content = self.bucket_service.get_media(filename)
            return base64_content
        except Exception as e:
            raise ValueError(f"Failed to retrieve audio from bucket: {str(e)}")

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
    def possible_audio(raw_k):
        lower = raw_k.lower()
        return (
            "audio" in lower
            or "sound" in lower
            or "voice" in lower
            or "speech" in lower
            or "recording" in lower
        )

    @staticmethod
    def infer_type(raw_k, raw_v):
        maybe_img = LogDAO.possible_img(raw_k)
        maybe_audio = LogDAO.possible_audio(raw_k)
        if isinstance(raw_v, str):
            try:
                if _is_time_string(raw_v):
                    return "time"
                if _is_date_string(raw_v):
                    return "date"
                if _is_timedelta_string(raw_v):
                    return "timedelta"

                datetime.fromisoformat(raw_v)
                return "datetime"
            except:
                lower_v = raw_v.lower()
                if lower_v.endswith((".mp3", ".wav")):
                    return "audio"
                if not maybe_img and not maybe_audio:
                    return "str"

                binary = raw_v.encode("utf-8")
                try:
                    assert base64.b64encode(base64.b64decode(binary)) == binary
                    if maybe_audio:
                        return "audio"
                    if maybe_img:
                        return "image"
                except:
                    if (
                        maybe_audio
                        and lower_v.startswith("http")
                        and (lower_v.endswith(".mp3") or lower_v.endswith(".wav"))
                    ):
                        return "audio"
                    if (
                        maybe_img
                        and lower_v.startswith("http")
                        and (
                            lower_v.endswith(".png")
                            or lower_v.endswith(".jpg")
                            or lower_v.endswith(".jpeg")
                        )
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

        # Start with a query for log events; apply project filter if provided
        query = select(LogEvent.id)
        if project_id is not None:
            query = query.where(LogEvent.project_id == project_id)

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
            # First join to LogEventLog to get log_event_id associations
            log_event_log_table = alias(LogEventLog.__table__, f"log_event_log_{idx}")
            query = query.join(
                log_event_log_table,
                log_event_log_table.c.log_event_id == LogEvent.id,
            ).join(
                log_table,
                log_table.c.id == log_event_log_table.c.log_id,
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

        query = (
            select(Log, LogEvent.created_at.label("log_event_ts"))
            .join(
                LogEventLog,
                LogEventLog.log_id == Log.id,
            )
            .join(
                LogEvent,
                LogEvent.id == LogEventLog.log_event_id,
            )
        )
        if id:
            query = query.where(Log.id.in_(id))
        if log_event_id:
            query = query.where(LogEventLog.log_event_id.in_(log_event_id))
        if key:
            query = query.where(Log.key.in_(key))
        if value:
            cast_values = [cast(literal(val), JSONB) for val in value]
            query = query.where(Log.value.in_(cast_values))
        if version:
            query = query.where(Log.param_version.in_(version))
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

            # Update Log table via LogEventLog association
            # First find the log IDs to update
            log_ids_to_update = (
                self.session.query(Log.id)
                .join(LogEventLog, LogEventLog.log_id == Log.id)
                .filter(
                    LogEventLog.log_event_id.in_(log_event_ids),
                    Log.key == old_field_name,
                )
                .subquery()
            )

            # Then update without joins
            log_update = (
                self.session.query(Log)
                .filter(Log.id.in_(select(log_ids_to_update)))
                .update(
                    {"key": new_field_name, "updated_at": datetime.now(timezone.utc)},
                    synchronize_session=False,
                )
            )

            # Update JSONLog table via LogEventJSONLog association
            # First find the JSON log IDs to update
            json_log_ids_to_update = (
                self.session.query(JSONLog.id)
                .join(LogEventJSONLog, LogEventJSONLog.json_log_id == JSONLog.id)
                .filter(
                    LogEventJSONLog.log_event_id.in_(log_event_ids),
                    JSONLog.key == old_field_name,
                )
                .subquery()
            )

            # Then update without joins
            json_logs_to_update = (
                self.session.query(JSONLog)
                .filter(JSONLog.id.in_(select(json_log_ids_to_update)))
                .update({"key": new_field_name}, synchronize_session=False)
            )

            self.session.commit()

        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to rename field: {str(e)}")

    def _bulk_delete_gcs_media(self, logs_query: Query):
        """
        Finds all image/audio logs in a given query and deletes the
        corresponding files from GCS.
        """
        gcs_url_prefix = (
            f"https://storage.googleapis.com/{self.bucket_service.bucket_name}/"
        )

        # Filter the query to only include logs that might have GCS files
        media_logs_query = logs_query.filter(
            Log.inferred_type.in_(("image", "audio")),
        )

        logs_to_delete = media_logs_query.all()
        if not logs_to_delete:
            return

        logging.info(
            f"Found {len(logs_to_delete)} media log(s) to check for GCS deletion.",
        )

        for log in logs_to_delete:
            if isinstance(log.value, str):
                # Strip potential quotes from JSONB string literal
                clean_value = log.value.strip("\"'")
                if clean_value.startswith(gcs_url_prefix):
                    try:
                        filename = clean_value.split("/")[-1]
                        logging.warning(
                            f"Deleting GCS file: {filename} for log ID: {log.id}",
                        )
                        self.bucket_service.delete_media(filename)
                    except Exception as e:
                        # Log the error but don't stop the overall delete process
                        logging.error(
                            f"Failed to delete file from GCS for log {log.id}: {str(e)}",
                        )

    def delete(self, id: int):
        """Deletes a single Log record and its associated GCS file if applicable."""
        try:
            log_query = self.session.query(Log).filter_by(id=id)
            log = log_query.one_or_none()

            if not log:
                raise ValueError(f"Log with id {id} not found.")

            # Call the bulk helper to handle GCS deletion
            self._bulk_delete_gcs_media(log_query)

            # Delete corresponding JSONLog if it exists
            # First get the log_event_id from LogEventLog association
            log_event_log = (
                self.session.query(LogEventLog).filter_by(log_id=log.id).first()
            )

            if log_event_log:
                # Find JSONLog via LogEventJSONLog association
                json_log = (
                    self.session.query(JSONLog)
                    .join(LogEventJSONLog, LogEventJSONLog.json_log_id == JSONLog.id)
                    .filter(
                        LogEventJSONLog.log_event_id == log_event_log.log_event_id,
                        JSONLog.key == log.key,
                    )
                    .first()
                )
            else:
                json_log = None
            if json_log:
                self.session.delete(json_log)

            # Delete the log record itself
            self.session.delete(log)
            self.session.commit()

        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete log with id {id}: {e}")

    def _check_uniqueness(self, entries: List[Dict[str, Any]]):
        unique_field_defs = {}  # (project_id, context_id, key) -> FieldType

        # Collect all project and context IDs from entries
        all_project_ids = set(e["project_id"] for e in entries if "project_id" in e)
        if not all_project_ids:
            return

        # Fetch all unique field definitions for these projects
        field_types = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id.in_(all_project_ids),
                FieldType.unique == True,
            )
            .all()
        )

        for ft in field_types:
            unique_field_defs[(ft.project_id, ft.context_id, ft.field_name)] = ft

        if not unique_field_defs:
            return

        # Group entries by project and context to handle composite keys correctly
        grouped_by_context = defaultdict(list)
        for entry in entries:
            # We only care about entries that correspond to a unique field
            if (
                entry.get("project_id"),
                entry.get("context_id"),
                entry.get("key"),
            ) in unique_field_defs:
                grouped_by_context[
                    (entry.get("project_id"), entry.get("context_id"))
                ].append(entry)

        if not grouped_by_context:
            return

        # Check for duplicates within the batch first
        batch_unique_values = defaultdict(set)
        for (project_id, context_id), context_entries in grouped_by_context.items():
            context = (
                self.session.query(Context).filter_by(id=context_id).one_or_none()
                if context_id
                else None
            )
            composite_keys = (
                context.unique_id_names
                if context
                and context.unique_id_names
                and len(context.unique_id_names) > 1
                else None
            )

            if composite_keys:
                # Group by log_event_id for composite check
                log_event_composites = defaultdict(dict)
                for entry in context_entries:
                    if entry["key"] in composite_keys:
                        log_event_composites[entry["log_event_id"]][
                            entry["key"]
                        ] = entry["value"]
                for log_event_id, kv_pair in log_event_composites.items():
                    # Create a frozenset of items to make it hashable for the batch check
                    composite_val = frozenset(kv_pair.items())
                    if composite_val in batch_unique_values[(project_id, context_id)]:
                        raise ValueError(
                            f"Duplicate entry for composite key {kv_pair} in batch.",
                        )
                    batch_unique_values[(project_id, context_id)].add(composite_val)
            else:
                # Simple key check
                for entry in context_entries:
                    simple_val = entry["value"]
                    if (
                        simple_val
                        in batch_unique_values[(project_id, context_id, entry["key"])]
                    ):
                        raise ValueError(
                            f"Duplicate value for unique field '{entry['key']}' in batch.",
                        )
                    batch_unique_values[(project_id, context_id, entry["key"])].add(
                        simple_val,
                    )

        # Check against DB
        for (project_id, context_id), context_entries in grouped_by_context.items():
            context = (
                self.session.query(Context).filter_by(id=context_id).one_or_none()
                if context_id
                else None
            )
            composite_keys = (
                context.unique_id_names
                if context
                and context.unique_id_names
                and len(context.unique_id_names) > 1
                else None
            )

            if composite_keys:
                # Handle composite key check
                log_events_to_check = defaultdict(dict)
                for entry in context_entries:
                    if entry["key"] in composite_keys:
                        log_events_to_check[entry["log_event_id"]][
                            entry["key"]
                        ] = entry["value"]

                for log_event_id, key_values in log_events_to_check.items():
                    if len(key_values) != len(composite_keys):
                        continue

                    # Find existing log_events that have the exact same set of key-value pairs
                    q = (
                        select(LogEventLog.log_event_id)
                        .join(Log, Log.id == LogEventLog.log_id)
                        .join(
                            LogEventContext,
                            LogEventContext.log_event_id == LogEventLog.log_event_id,
                        )
                        .where(LogEventContext.context_id == context_id)
                        .where(
                            or_(
                                *[
                                    and_(
                                        Log.key == k,
                                        Log.value == literal(v, type_=JSONB),
                                    )
                                    for k, v in key_values.items()
                                ],
                            ),
                        )
                        .group_by(LogEventLog.log_event_id)
                        .having(func.count(Log.id) == len(composite_keys))
                    )

                    if self.session.execute(q.limit(1)).first():
                        raise ValueError(
                            f"Duplicate entry for composite key {key_values}.",
                        )
            else:
                # Handle simple unique key check (original logic, but scoped to this context group)
                keys_and_values = defaultdict(list)
                for entry in context_entries:
                    keys_and_values[entry["key"]].append(entry["value"])

                for key, values in keys_and_values.items():
                    q = (
                        select(Log.id)
                        .join(LogEventLog, LogEventLog.log_id == Log.id)
                        .join(LogEvent, LogEvent.id == LogEventLog.log_event_id)
                        .join(
                            LogEventContext,
                            LogEvent.id == LogEventContext.log_event_id,
                        )
                        .where(
                            LogEvent.project_id == project_id,
                            Log.key == key,
                            Log.value.in_([literal(v, type_=JSONB) for v in values]),
                        )
                    )
                    if context_id is not None:
                        q = q.where(LogEventContext.context_id == context_id)

                    if self.session.execute(q.limit(1)).first():
                        raise ValueError(f"Duplicate entry for unique field '{key}'.")

    def bulk_create(
        self,
        entries: List[Dict[str, Any]],
        context_obj: Context | None = None,
    ) -> List[int]:
        """
        Create multiple Log entries in a single database transaction.

        Args:
            entries: List of dictionaries with the following keys:
                - project_id: int
                - log_event_id: int (will create LogEventLog association)
                - key: str
                - value: Any (optional)
                - param_version: int (optional)
                - explicit_types: Dict (optional)
                - context_id: int (optional)
            context_obj: Optional Context object. When provided, enforces that all entries
                belong to this single context (one-context-per-batch requirement).

        Returns:
            List of created log IDs

        Note:
            This method flushes then commits the transaction. When context_obj is provided,
            only updates context_obj.updated_at (version bump happens elsewhere).
        """
        if not entries:
            return []

        self._check_uniqueness(entries)

        # Enforce one-context-per-batch requirement when context_obj is provided
        if context_obj:
            for entry in entries:
                entry_context_id = entry.get("context_id")
                if entry_context_id is not None and entry_context_id != context_obj.id:
                    raise ValueError(
                        f"Entry context_id {entry_context_id} does not match provided context_obj.id {context_obj.id}",
                    )

        # Start transaction
        rows_json: list[dict] = []
        rows_log_versioned: list[dict] = []
        rows_log_versioned_pk2val: dict[tuple[int, str, int], Any] = {}
        rows_json_pk2val: dict[tuple[int, str], Any] = {}
        pending_log_rows: list[dict] = []

        try:
            now = datetime.now(timezone.utc)

            # Process each entry
            for entry in entries:
                project_id = entry.get("project_id")
                log_event_id = entry.get("log_event_id")
                key = entry.get("key")
                value = entry.get("value")
                param_version = entry.get("param_version")
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

                # Handle image and audio uploads
                if (
                    inferred_type == "image"
                    and isinstance(value, str)
                    and not value.lower().startswith("http")
                ):
                    value = self.upload_image_to_bucket(value)
                elif (
                    inferred_type == "audio"
                    and isinstance(value, str)
                    and not value.lower().endswith((".mp3", ".wav"))
                    and not value.lower().startswith("http")
                ):
                    value = self.upload_audio_to_bucket(value)
                if inferred_type == "datetime" and isinstance(value, str):
                    value = normalize_timestamp(value)

                # Collect JSON-typed entries so we can insert them in one statement after the loop
                if isinstance(value, (dict, list)):
                    pk = (log_event_id, key)
                    if pk in rows_json_pk2val and rows_json_pk2val[pk] != value:
                        raise OverwriteError(
                            f"Conflicting JSON values for key '{key}' in same batch",
                        )
                    rows_json_pk2val[pk] = value
                    rows_json.append(
                        {
                            "log_event_id": log_event_id,  # Store temporarily for association
                            "key": key,
                            "value": value,
                        },
                    )

                # Create Log entry
                if param_version is not None:
                    pk_v = (log_event_id, key, param_version)
                    if (
                        pk_v in rows_log_versioned_pk2val
                        and rows_log_versioned_pk2val[pk_v] != value
                    ):
                        raise OverwriteError(
                            f"Conflicting values for key '{key}', param_version {param_version} "
                            "in same batch",
                        )
                    rows_log_versioned_pk2val[pk_v] = value
                    rows_log_versioned.append(
                        {
                            "log_event_id": log_event_id,
                            "key": key,
                            "value": value,
                            "param_version": param_version,
                            "inferred_type": inferred_type,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                else:
                    pending_log_rows.append(
                        {
                            "log_event_id": log_event_id,
                            "key": key,
                            "value": value,
                            "param_version": None,
                            "inferred_type": inferred_type,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )

            # Bulk-insert the accumulated rows in **one** statement
            # First, we need to create Logs without log_event_id
            if pending_log_rows:
                # Prepare log rows without log_event_id
                log_rows_to_insert = []
                log_event_associations = (
                    []
                )  # Track which log_event_id goes with each log

                for row in pending_log_rows:
                    log_event_id = row.pop("log_event_id")  # Remove log_event_id
                    log_rows_to_insert.append(row)
                    log_event_associations.append(log_event_id)

                # Insert logs and get their IDs
                stmt = pg_insert(Log).values(log_rows_to_insert).returning(Log.id)
                result = self.session.execute(stmt)
                log_ids = [row[0] for row in result]

                # Create LogEventLog associations
                log_event_log_rows = []
                for log_id, log_event_id in zip(log_ids, log_event_associations):
                    log_event_log_rows.append(
                        {
                            "log_event_id": log_event_id,
                            "log_id": log_id,
                        },
                    )

                if log_event_log_rows:
                    stmt_assoc = pg_insert(LogEventLog).values(log_event_log_rows)
                    self.session.execute(stmt_assoc)

            # Bulk-insert versioned rows in **one** statement
            if rows_log_versioned:
                # 1. pre-check for conflicting rows already in the DB
                pks_v = list(rows_log_versioned_pk2val.keys())

                # Check for existing logs with the same key and param_version via LogEventLog
                log_event_ids_to_check = [pk[0] for pk in pks_v]
                keys_to_check = [pk[1] for pk in pks_v]
                versions_to_check = [pk[2] for pk in pks_v]

                rows_to_check = (
                    self.session.query(Log, LogEventLog.log_event_id)
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(LogEventLog.log_event_id.in_(log_event_ids_to_check))
                    .filter(Log.key.in_(keys_to_check))
                    .filter(Log.param_version.in_(versions_to_check))
                    .with_for_update()
                    .all()
                )
                for row, log_event_id in rows_to_check:
                    intended = rows_log_versioned_pk2val[
                        (log_event_id, row.key, row.param_version)
                    ]
                    if row.value != intended:
                        raise OverwriteError(
                            f"Cannot overwrite existing value for key "
                            f"'{row.key}', param_version {row.param_version}",
                        )

                # Prepare versioned log rows without log_event_id
                versioned_log_rows_to_insert = []
                versioned_log_event_associations = []

                for row in rows_log_versioned:
                    log_event_id = row.pop("log_event_id")  # Remove log_event_id
                    versioned_log_rows_to_insert.append(row)
                    versioned_log_event_associations.append(log_event_id)

                # Insert versioned logs and get their IDs
                stmt_v = (
                    pg_insert(Log)
                    .values(versioned_log_rows_to_insert)
                    .returning(Log.id)
                )
                result_v = self.session.execute(stmt_v)
                versioned_log_ids = [row[0] for row in result_v]

                # Create LogEventLog associations for versioned logs
                versioned_log_event_log_rows = []
                for log_id, log_event_id in zip(
                    versioned_log_ids,
                    versioned_log_event_associations,
                ):
                    versioned_log_event_log_rows.append(
                        {
                            "log_event_id": log_event_id,
                            "log_id": log_id,
                        },
                    )

                if versioned_log_event_log_rows:
                    stmt_v_assoc = pg_insert(LogEventLog).values(
                        versioned_log_event_log_rows,
                    )
                    self.session.execute(stmt_v_assoc)

            # Detect JSON conflicts first
            if rows_json_pk2val:
                pks = list(rows_json_pk2val.keys())
                # Check for existing JSONLog entries by joining through LogEventJSONLog
                conflicting = (
                    self.session.query(JSONLog, LogEventJSONLog.log_event_id)
                    .join(LogEventJSONLog, LogEventJSONLog.json_log_id == JSONLog.id)
                    .filter(LogEventJSONLog.log_event_id.in_([pk[0] for pk in pks]))
                    .filter(JSONLog.key.in_([pk[1] for pk in pks]))
                    .with_for_update()
                    .all()
                )
                for json_log, log_event_id in conflicting:
                    intended = rows_json_pk2val[(log_event_id, json_log.key)]
                    if json_log.value != intended:
                        raise OverwriteError(
                            f"Cannot overwrite existing JSON value for key '{json_log.key}'",
                        )

            # Single multi-row INSERT for JSONLog
            if rows_json:
                # Prepare JSONLog rows without log_event_id
                json_log_rows_to_insert = []
                json_log_event_associations = (
                    []
                )  # Track which log_event_id goes with each JSONLog

                for row in rows_json:
                    log_event_id = row.pop("log_event_id")  # Remove log_event_id
                    json_log_rows_to_insert.append(row)
                    json_log_event_associations.append(log_event_id)

                # Insert JSONLogs and get their IDs
                stmt_json = (
                    pg_insert(JSONLog)
                    .values(json_log_rows_to_insert)
                    .returning(JSONLog.id)
                )
                result_json = self.session.execute(stmt_json)
                json_log_ids = [row[0] for row in result_json]

                # Create LogEventJSONLog associations
                log_event_json_log_rows = []
                for json_log_id, log_event_id in zip(
                    json_log_ids,
                    json_log_event_associations,
                ):
                    log_event_json_log_rows.append(
                        {
                            "log_event_id": log_event_id,
                            "json_log_id": json_log_id,
                        },
                    )

                if log_event_json_log_rows:
                    stmt_json_assoc = pg_insert(LogEventJSONLog).values(
                        log_event_json_log_rows,
                    )
                    self.session.execute(stmt_json_assoc)

            self.session.flush()

        except Exception as e:
            raise e

    def get_next_row_ids(
        self,
        project_id: int,
        context_id: int,
        param_key: str,
        count: int,
    ) -> List[int]:
        """
        Atomically obtains and increments the version counter for a given parameter key
        to reserve a batch of sequential IDs.

        Args:
            project_id: The project identifier.
            context_id: The context identifier.
            param_key: The parameter key for the unique ID column.
            count: The number of sequential IDs to reserve.

        Returns:
            A list of the next sequential IDs.
        """
        if count == 0:
            return []

        # Use pg_insert with ON CONFLICT DO NOTHING to ensure the counter row exists.
        # This is safe for concurrent calls.
        insert_stmt = (
            pg_insert(ParamVersion)
            .values(
                project_id=project_id,
                context_id=context_id,
                param_key=param_key,
                last_version=-1,  # Initialize with 0
            )
            .on_conflict_do_nothing(
                index_elements=["project_id", "context_id", "param_key"],
            )
        )
        self.session.execute(insert_stmt)

        # Atomically increment the counter by the batch size and return the new value.
        # The database ensures this operation is safe from race conditions.
        result = self.session.execute(
            text(
                """
                UPDATE param_version
                SET last_version = last_version + :count
                WHERE project_id = :project_id AND context_id = :context_id AND param_key = :param_key
                RETURNING last_version
            """,
            ),
            {
                "project_id": project_id,
                "context_id": context_id,
                "param_key": param_key,
                "count": count,
            },
        )

        new_max_id = result.scalar_one_or_none()

        if new_max_id is None:
            self.session.rollback()
            raise ValueError(
                f"Failed to get next batch of row IDs for parameter {param_key}",
            )

        # Calculate the range of IDs reserved in this batch
        start_id = new_max_id - count + 1
        return list(range(start_id, new_max_id + 1))

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

    def _validate_parent_exists(
        self,
        context_id: int,
        parent_ids: Dict[str, Any],
    ) -> bool:
        """
        Validates that a log with the given parent ID combination already exists in the context.
        """
        if not parent_ids:
            return True

        conditions = [
            and_(Log.key == k, Log.value == literal(v, type_=JSONB))
            for k, v in parent_ids.items()
        ]

        q = (
            select(LogEventLog.log_event_id)
            .join(Log, Log.id == LogEventLog.log_id)
            .join(
                LogEventContext,
                LogEventContext.log_event_id == LogEventLog.log_event_id,
            )
            .where(LogEventContext.context_id == context_id, or_(*conditions))
            .group_by(LogEventLog.log_event_id)
            .having(func.count(Log.id) == len(parent_ids))
        )
        return self.session.execute(q.limit(1)).first() is not None

    def get_next_nested_ids(
        self,
        project_id: int,
        context_id: int,
        columns: List[str],
        provided_ids: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Generates nested, hierarchical unique IDs based on the strict sequential policy.
        """
        if not provided_ids:
            return []

        parent_id_dict = provided_ids[0]
        num_to_generate = len(provided_ids)

        target_col_index = len(parent_id_dict)
        if target_col_index >= len(columns):
            raise ValueError("All unique ID components have been specified.")
        target_col = columns[target_col_index]

        if not self._validate_parent_exists(context_id, parent_id_dict):
            raise ValueError(
                f"Parent ID combination {parent_id_dict} does not exist in this context.",
            )

        key_parts = [f"{col}={val}" for col, val in parent_id_dict.items()]
        key_parts.append(target_col)
        param_key = "::".join(key_parts)

        new_ids = self.get_next_row_ids(
            project_id=project_id,
            context_id=context_id,
            param_key=param_key,
            count=num_to_generate,
        )

        completed_ids = []
        for i in range(num_to_generate):
            final_id = parent_id_dict.copy()
            final_id[target_col] = new_ids[i]
            for j in range(target_col_index + 1, len(columns)):
                final_id[columns[j]] = 0

            # Initialize counters for all newly created child paths.
            temp_parent_path = {}
            for k in range(len(columns) - 1):
                current_level_key = columns[k]
                temp_parent_path[current_level_key] = final_id[current_level_key]

                child_col = columns[k + 1]
                child_key_parts = [
                    f"{col}={val}" for col, val in temp_parent_path.items()
                ]
                child_key_parts.append(child_col)
                child_param_key = "::".join(child_key_parts)

                # Use a simple ON CONFLICT DO NOTHING. This ensures that if the counter
                # already exists (from a previous operation), we don't touch it. If it
                # doesn't exist, we create it and mark the '0' ID as used.
                init_stmt = (
                    pg_insert(ParamVersion)
                    .values(
                        project_id=project_id,
                        context_id=context_id,
                        param_key=child_param_key,
                        last_version=0,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["project_id", "context_id", "param_key"],
                    )
                )
                self.session.execute(init_stmt)

            completed_ids.append(final_id)

        return completed_ids

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
                - param_version: int (optional)
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

        self._check_uniqueness(updates)
        field_types = field_types or {}

        try:
            now = datetime.now(timezone.utc)

            # Group updates by log_event_id and key for efficient querying
            update_groups = {}
            for update_item in updates:
                log_event_id = update_item.get("log_event_id")
                key = update_item.get("key")
                if not log_event_id or not key:
                    continue

                group_key = (log_event_id, key)
                update_groups[group_key] = update_item

            if not update_groups:
                return

            # Query all existing logs in one go
            log_event_ids = [k[0] for k in update_groups.keys()]
            keys = [k[1] for k in update_groups.keys()]
            existing_logs = (
                self.session.query(Log, LogEventLog.log_event_id)
                .join(LogEventLog, LogEventLog.log_id == Log.id)
                .filter(LogEventLog.log_event_id.in_(log_event_ids))
                .filter(Log.key.in_(keys))
                .all()
            )

            # Create a lookup for existing logs
            existing_log_map = {
                (log_event_id, log.key): log for log, log_event_id in existing_logs
            }

            # Query all existing JSON logs in one go via LogEventJSONLog association
            existing_json_logs = (
                self.session.query(JSONLog, LogEventJSONLog.log_event_id)
                .join(LogEventJSONLog, LogEventJSONLog.json_log_id == JSONLog.id)
                .filter(LogEventJSONLog.log_event_id.in_(log_event_ids))
                .filter(JSONLog.key.in_(keys))
                .all()
            )

            # Create a lookup for existing JSON logs
            existing_json_log_map = {
                (log_event_id, json_log.key): json_log
                for json_log, log_event_id in existing_json_logs
            }

            log_event_ids_to_update = set()

            # Collect rows for batch operations and conflict detection
            rows_log = []
            rows_json = []
            rows_log_pk2val = {}  # Maps (log_event_id, key, param_version) to value
            rows_json_pk2val = {}  # Maps (log_event_id, key) to value

            # Process each update
            for group_key, update_data in update_groups.items():
                log_event_id, key = group_key
                value = update_data.get("value")
                param_version = update_data.get("param_version")
                explicit_types = update_data.get("explicit_types", {})
                key_explicit_type = explicit_types.get(key, {})
                inferred_type = key_explicit_type.get("type")
                context_id = update_data.get("context_id")

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

                # Handle image and audio uploads
                json_value = value
                if (
                    inferred_type == "image"
                    and isinstance(value, str)
                    and not value.lower().startswith("http")
                ):
                    json_value = self.upload_image_to_bucket(value)
                elif (
                    inferred_type == "audio"
                    and isinstance(value, str)
                    and not value.lower().endswith((".mp3", ".wav"))
                    and not value.lower().startswith("http")
                ):
                    json_value = self.upload_audio_to_bucket(value)

                # Check if log exists
                existing_log = existing_log_map.get(group_key)

                if existing_log:
                    # Check if overwrite is allowed
                    if not update_data.get("overwrite", overwrite):
                        raise OverwriteError

                    # Check if field is immutable
                    if key in field_types and context_id is not None:
                        field_info = field_types.get(key)
                        if field_info and not field_info.get("mutable", False):
                            raise ImmutableFieldError

                    # Update existing log
                    existing_log.value = json_value
                    existing_log.param_version = param_version
                    existing_log.inferred_type = inferred_type
                    existing_log.updated_at = now

                    # Also update corresponding JSONLog
                    existing_json_log = existing_json_log_map.get(group_key)
                    if existing_json_log:
                        existing_json_log.value = json_value
                else:
                    # Prepare for batch upsert
                    log_pk = (log_event_id, key, param_version)
                    rows_log_pk2val[log_pk] = json_value
                    rows_log.append(
                        {
                            "_log_event_id": log_event_id,  # Track which log_event_id this belongs to
                            "key": key,
                            "value": json_value,
                            "param_version": param_version,
                            "inferred_type": inferred_type,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )

                # Handle JSON logs for dict/list values
                if isinstance(value, (dict, list)):
                    json_pk = (log_event_id, key)
                    rows_json_pk2val[json_pk] = json_value
                    rows_json.append(
                        {
                            "_log_event_id": log_event_id,  # Track which log_event_id this belongs to
                            "key": key,
                            "value": json_value,
                        },
                    )

                # Track log events to update timestamps
                log_event_ids_to_update.add(log_event_id)

            # Perform conflict detection if overwrite is False
            if not overwrite:
                # Check Log conflicts
                if rows_log_pk2val:
                    log_pks = list(rows_log_pk2val.keys())
                    log_event_ids_check = [pk[0] for pk in log_pks]
                    keys_check = [pk[1] for pk in log_pks]
                    versions_check = [pk[2] for pk in log_pks]

                    existing_conflicting_logs = (
                        self.session.query(Log, LogEventLog.log_event_id)
                        .join(LogEventLog, LogEventLog.log_id == Log.id)
                        .filter(LogEventLog.log_event_id.in_(log_event_ids_check))
                        .filter(Log.key.in_(keys_check))
                        .filter(Log.param_version.in_(versions_check))
                        .with_for_update()
                        .all()
                    )

                    for existing_log, log_event_id in existing_conflicting_logs:
                        pk = (
                            log_event_id,
                            existing_log.key,
                            existing_log.param_version,
                        )
                        intended_value = rows_log_pk2val.get(pk)
                        if (
                            intended_value is not None
                            and existing_log.value != intended_value
                        ):
                            raise OverwriteError(
                                f"Cannot overwrite existing value for key '{existing_log.key}'",
                            )

                # Check JSONLog conflicts
                if rows_json_pk2val:
                    json_pks = list(rows_json_pk2val.keys())
                    json_log_event_ids_check = [pk[0] for pk in json_pks]
                    json_keys_check = [pk[1] for pk in json_pks]

                    existing_conflicting_json_logs = (
                        self.session.query(JSONLog, LogEventJSONLog.log_event_id)
                        .join(
                            LogEventJSONLog,
                            LogEventJSONLog.json_log_id == JSONLog.id,
                        )
                        .filter(
                            LogEventJSONLog.log_event_id.in_(json_log_event_ids_check),
                        )
                        .filter(JSONLog.key.in_(json_keys_check))
                        .with_for_update()
                        .all()
                    )

                    for (
                        existing_json_log,
                        log_event_id,
                    ) in existing_conflicting_json_logs:
                        pk = (log_event_id, existing_json_log.key)
                        intended_value = rows_json_pk2val.get(pk)
                        if (
                            intended_value is not None
                            and existing_json_log.value != intended_value
                        ):
                            raise OverwriteError(
                                f"Cannot overwrite existing JSON value for key '{existing_json_log.key}'",
                            )

            # Single multi-row UPSERT for LOG
            if rows_log:
                # Build ordered list of (log_event_id, row_data) tuples
                log_event_rows = []
                for log_row in rows_log:
                    # Extract the tracked log_event_id
                    log_event_id = log_row.pop("_log_event_id")
                    log_event_rows.append((log_event_id, log_row))

                # Extract all unique (log_event_id, key, param_version) combinations
                check_keys = [
                    (eid, row["key"], row.get("param_version"))
                    for eid, row in log_event_rows
                ]

                # Bulk query to find all existing logs for these log_events
                # This query finds logs that match our (log_event_id, key, param_version) tuples
                existing_logs_query = (
                    self.session.query(
                        Log,
                        LogEventLog.log_event_id,
                    )
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(
                        or_(
                            *[
                                and_(
                                    LogEventLog.log_event_id == eid,
                                    Log.key == k,
                                    Log.param_version == pv,
                                )
                                for eid, k, pv in check_keys
                            ],
                        ),
                    )
                )

                # Build a map of (log_event_id, key, param_version) -> Log
                existing_map = {}
                for log, log_event_id in existing_logs_query:
                    key = (log_event_id, log.key, log.param_version)
                    existing_map[key] = log

                # Process updates based on overwrite flag
                logs_to_insert = []
                insert_event_ids = []

                for i, (log_event_id, log_row) in enumerate(log_event_rows):
                    lookup_key = (
                        log_event_id,
                        log_row["key"],
                        log_row.get("param_version"),
                    )
                    existing_log = existing_map.get(lookup_key)

                    if existing_log:
                        if overwrite:
                            # Update existing log
                            existing_log.value = log_row["value"]
                            existing_log.inferred_type = log_row["inferred_type"]
                            existing_log.updated_at = now
                        # else: skip (don't overwrite)
                    else:
                        # Need to create new log
                        logs_to_insert.append(log_row)
                        insert_event_ids.append(log_event_id)

                # Bulk insert new logs
                if logs_to_insert:
                    stmt = pg_insert(Log).values(logs_to_insert).returning(Log.id)
                    result = self.session.execute(stmt)
                    new_log_ids = [row[0] for row in result]

                    # Create associations for new logs
                    log_event_log_rows = []
                    for log_id, log_event_id in zip(new_log_ids, insert_event_ids):
                        log_event_log_rows.append(
                            {
                                "log_event_id": log_event_id,
                                "log_id": log_id,
                            },
                        )

                    if log_event_log_rows:
                        stmt_assoc = pg_insert(LogEventLog).values(log_event_log_rows)
                        self.session.execute(stmt_assoc)

            # Handle JSONLog updates with many-to-many relationship
            if rows_json:
                # Build ordered list of (log_event_id, json_row) tuples
                json_event_rows = []
                for json_row in rows_json:
                    # Extract the tracked log_event_id
                    log_event_id = json_row.pop("_log_event_id")
                    json_event_rows.append((log_event_id, json_row))

                # Process based on overwrite flag
                json_logs_to_insert = []
                insert_json_event_ids = []

                for log_event_id, json_row in json_event_rows:
                    existing_json_log = existing_json_log_map.get(
                        (log_event_id, json_row["key"]),
                    )

                    if existing_json_log:
                        if overwrite:
                            # Update existing JSONLog value
                            existing_json_log.value = json_row["value"]
                        # else: skip (don't overwrite)
                    else:
                        # Need to create new JSONLog
                        json_logs_to_insert.append(json_row)
                        insert_json_event_ids.append(log_event_id)

                # Bulk insert new JSON logs
                if json_logs_to_insert:
                    stmt = (
                        pg_insert(JSONLog)
                        .values(json_logs_to_insert)
                        .returning(JSONLog.id)
                    )
                    result = self.session.execute(stmt)
                    new_json_log_ids = [row[0] for row in result]

                    # Create associations for new JSON logs
                    json_log_event_log_rows = []
                    for json_log_id, log_event_id in zip(
                        new_json_log_ids,
                        insert_json_event_ids,
                    ):
                        json_log_event_log_rows.append(
                            {
                                "log_event_id": log_event_id,
                                "json_log_id": json_log_id,
                            },
                        )

                    if json_log_event_log_rows:
                        stmt_assoc = pg_insert(LogEventJSONLog).values(
                            json_log_event_log_rows,
                        )
                        self.session.execute(stmt_assoc)

            # Bulk update log event timestamps
            if log_event_ids_to_update:
                stmt = (
                    update(LogEvent)
                    .where(LogEvent.id.in_(log_event_ids_to_update))
                    .values(updated_at=now)
                )
                self.session.execute(stmt)

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
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(LogEventLog.log_event_id == le_id, Log.key == base_key)
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

                # Get the corresponding JSONLog if it exists via LogEventJSONLog association
                json_log = (
                    self.session.query(JSONLog)
                    .join(LogEventJSONLog, LogEventJSONLog.json_log_id == JSONLog.id)
                    .filter(
                        LogEventJSONLog.log_event_id == le_id,
                        JSONLog.key == base_key,
                    )
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
