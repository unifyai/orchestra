import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends
from sqlalchemy import cast, literal, select
from sqlalchemy.dialects.postgresql import JSONB
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
        value: Optional[str] = None,  # JSON serialised
        version: Optional[int] = None,
        inferred_type: Optional[str] = None,
        context_id: Optional[int] = None,
    ) -> int:
        if isinstance(value, (dict, list)):
            # for dicts and lists, we use JSONLog to preserve ordering
            json_log = JSONLog(
                log_event_id=log_event_id,
                key=key,
                value=value,
            )
            self.session.add(json_log)

        # Handle versioned history
        self._handle_versioned_history(
            context_id=context_id,
            log_event_id=log_event_id,
            key=key,
            value=value,
            inferred_type=inferred_type,
            description=f"Created entry with key {key}",
            json_value=value,
        )

        if version:
            # Lock the rows for version check
            query = (
                select(Log)
                .join(LogEvent, Log.log_event_id == LogEvent.id)
                .where(
                    LogEvent.project_id == project_id,
                    Log.key == key,
                    Log.version == version,
                )
            )
            existing = self.session.execute(query).first()
            if existing and existing[0].value != value:
                raise ValueError("Version mismatch")

        # If the field is of type image and value is base64, upload it to the bucket
        if (
            inferred_type == "image"
            and isinstance(value, str)
            and not value.lower().startswith("http")
        ):
            value = self.upload_image_to_bucket(value)

        ts = datetime.now(timezone.utc)

        new_log = Log(
            log_event_id=log_event_id,
            key=key,
            value=value,
            version=version,
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
                    created_at=log.created_at,
                    updated_at=log.updated_at,
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
