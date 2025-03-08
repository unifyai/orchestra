import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends
from sqlalchemy import cast, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.dao.bucket_service import BucketService
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Context,
    JSONLog,
    JSONLogHistory,
    Log,
    LogEvent,
    LogEventContext,
    LogHistory,
)


class OverwriteError(Exception):
    pass


class ImmutableFieldError(Exception):
    pass


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

    def create(
        self,
        project_id: int,
        log_event_id: int,
        key: str,
        value: Optional[Any] = None,
        version: Optional[int] = None,
        inferred_type: Optional[str] = None,
        context_id: Optional[int] = None,
    ) -> int:
        """Create a new Log row and optionally a JSONLog row with concurrency control."""
        if (
            inferred_type == "image"
            and isinstance(value, str)
            and not value.lower().startswith("http")
        ):
            value = self.upload_image_to_bucket(value)

        if isinstance(value, (dict, list)):
            json_log = JSONLog(log_event_id=log_event_id, key=key, value=value)
            self.session.add(json_log)

        self._handle_versioned_history(
            context_id=context_id,
            log_event_id=log_event_id,
            key=key,
            value=value,
            inferred_type=inferred_type,
            description=f"Created entry with key {key}",
            json_value=value,
        )

        ts = datetime.now(timezone.utc)
        if version is not None:
            # Build an INSERT ... ON CONFLICT DO NOTHING for concurrency control.
            insert_stmt = (
                pg_insert(Log)
                .values(
                    log_event_id=log_event_id,
                    key=key,
                    version=version,
                    value=value,
                    inferred_type=inferred_type,
                    created_at=ts,
                    updated_at=ts,
                )
                .on_conflict_do_nothing(
                    index_elements=["log_event_id", "key", "version"],
                )
            )

            result = self.session.execute(insert_stmt)
            inserted_rows = (
                result.rowcount
            )  # 1 if new row inserted, 0 if conflict existed

            if inserted_rows == 1:
                # We successfully inserted a brand-new row, so fetch it back.
                new_log = (
                    self.session.query(Log)
                    .filter_by(log_event_id=log_event_id, key=key, version=version)
                    .one()
                )
            else:
                # Another thread/process already inserted (log_event_id, key, version).
                # Check if the existing row has the same value or not.
                existing_log = (
                    self.session.query(Log)
                    .filter_by(log_event_id=log_event_id, key=key, version=version)
                    .one()
                )
                if existing_log.value != value:
                    raise ValueError(
                        f"Version mismatch: Attempted to insert (log_event_id={log_event_id}, "
                        f"key='{key}', version={version}) with a different value.\n"
                        f"Existing: {existing_log.value}\nNew: {value}",
                    )
                # If the values match, do nothing and reuse the existing row
                new_log = existing_log

        else:
            new_log = Log(
                log_event_id=log_event_id,
                key=key,
                value=value,
                version=None,
                inferred_type=inferred_type,
                created_at=ts,
                updated_at=ts,
            )
            self.session.add(new_log)

        self.session.commit()
        return new_log.id

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
                datetime.fromisoformat(raw_v)
                return "timestamp"
            except:
                if _is_time_string(raw_v):
                    return "time"
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

    def create_from_raw_k_v(
        self,
        project_id: int,
        log_event_id: int,
        raw_k: str,
        version: Optional[int] = None,
        raw_v: Optional[Any] = None,
        explicit_types: Optional[Dict] = None,
        context_id: Optional[int] = None,
    ) -> Optional[str]:
        explicit_types = explicit_types if isinstance(explicit_types, dict) else {}

        return self.create(
            project_id=project_id,
            log_event_id=log_event_id,
            key=raw_k,
            value=raw_v,
            version=version,
            inferred_type=explicit_types.get(raw_k, {}).get(
                "type",
                self.infer_type(raw_k, raw_v),
            ),
            context_id=context_id,
        )

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

    def update_value(
        self,
        log_event_id: int,
        raw_k: str,
        raw_v: Optional[Any] = None,
        version: Optional[int] = None,
        explicit_types: Optional[Dict] = None,
        overwrite: bool = False,
        field_types: Optional[Dict] = None,
        context_id: Optional[int] = None,
    ):
        explicit_types = explicit_types if isinstance(explicit_types, dict) else {}
        inferred_type = explicit_types.get(raw_k, {}).get(
            "type",
            self.infer_type(raw_k, raw_v),
        )
        json_v = raw_v

        # If the field is image and raw_v is a base64 string, upload it
        if (
            inferred_type == "image"
            and isinstance(raw_v, str)
            and not raw_v.lower().startswith("http")
        ):
            json_v = self.upload_image_to_bucket(raw_v)

        query = (
            select(Log).where(Log.log_event_id == log_event_id).where(Log.key == raw_k)
        )
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        # Get the context before any updates
        context = self.session.query(Context).filter_by(id=context_id).first()
        if entry is not None:
            if not overwrite and hasattr(entry, "value"):
                raise OverwriteError
            if raw_k in field_types:
                if not context or not context.is_versioned:
                    field_info = field_types.get(raw_k)
                    if field_info and not field_info.get("mutable", False):
                        raise ImmutableFieldError

            setattr(entry, "value", json_v)
            setattr(entry, "version", version)
            setattr(entry, "inferred_type", inferred_type)

            # Handle regular log history first
            self._handle_versioned_history(
                context_id=context_id,
                log_event_id=entry.log_event_id,
                key=entry.key,
                value=entry.value,
                inferred_type=entry.inferred_type,
                description=f"Updated entry with key {raw_k}",
            )

            # Update the corresponding JSONLog row if needed.
            if isinstance(raw_v, (dict, list)):
                # for dicts and lists, we use JSONLog to preserve ordering
                json_query = select(JSONLog).where(
                    JSONLog.log_event_id == log_event_id,
                    JSONLog.key == raw_k,
                )
                json_raw = self.session.execute(json_query)
                json_entry = json_raw.scalars().first()

                # Update or create the JSONLog entry
                if json_entry:
                    json_entry.value = raw_v
                else:
                    new_json_log = JSONLog(
                        log_event_id=log_event_id,
                        key=raw_k,
                        value=raw_v,
                    )
                    self.session.add(new_json_log)

                # Handle JSON history separately since it needs special treatment
                if context and context.is_versioned:
                    if json_entry:
                        # Archive the current JSON value before updating
                        self._create_json_log_history(
                            log_event_id=log_event_id,
                            key=raw_k,
                            value=json_entry.value,  # Archive the old value
                            version=context.version,
                            description=f"Updated JSON entry with key {raw_k}",
                        )
                    else:
                        # If creating a new JSON entry in a versioned context, archive it
                        self._create_json_log_history(
                            log_event_id=log_event_id,
                            key=raw_k,
                            value=raw_v,  # Archive the new value
                            version=context.version,
                            description=f"Created JSON entry with key {raw_k}",
                        )

            log_event_query = (
                select(LogEvent).where(LogEvent.id == log_event_id).with_for_update()
            )
            log_event = self.session.execute(log_event_query).scalars().first()
            if log_event:
                log_event.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        else:
            raise IndexError

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
                inferred_type = entry.get("explicit_types", {}).get(key, {}).get("type")
                if inferred_type is None:
                    inferred_type = self.infer_type(key, value)
                context_id = entry.get("context_id")

                if not all([log_event_id, key]):
                    continue

                # Handle image uploads
                if (
                    inferred_type == "image"
                    and isinstance(value, str)
                    and not value.lower().startswith("http")
                ):
                    value = self.upload_image_to_bucket(value)

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
                context_id = update.get("context_id")

                # Determine inferred type
                inferred_type = explicit_types.get(key, {}).get("type")
                if inferred_type is None:
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
                    self.session.query(LogEvent)
                    .filter_by(id=log_event_id)
                    .with_for_update()
                    .first()
                )
                if log_event:
                    log_event.updated_at = now

            self.session.commit()

        except (OverwriteError, ImmutableFieldError):
            raise
        except Exception as e:
            raise ValueError(f"Failed to perform bulk update: {str(e)}")
