"""Async version of log_dao for use with AsyncSession."""
import base64
import copy
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import HTTPException
from sqlalchemy import alias, and_, cast, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
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


# noinspection PyBroadException
class AsyncLogDAO:
    def __init__(
        self,
        session: AsyncSession,
        context_dao: ContextDAO,
    ):
        self.session = session
        self.bucket_service = BucketService()
        self.context_dao = context_dao

    async def check_field_update(
        self,
        field_key: str,
        field_types: Dict[str, Any],
        explicit_types_dict: Dict[str, Any],
        is_nested: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Validate a field update for type compatibility and return field metadata.

        Args:
            field_key: The field name (base key for nested updates)
            field_types: Dictionary of existing field type definitions
            explicit_types_dict: User-provided explicit type overrides
            is_nested: Whether this is a nested path update (e.g., profile.age)

        Returns:
            None if validation fails (caller should skip this update)
            Dict with field metadata if validation passes:
                - exists: bool - whether field already exists
                - mutable: bool - mutability setting (for new fields)
                - unique: bool - uniqueness setting (for new fields)
                - field_type: str - explicit type (for new fields)
                - enum_values: list - enum values (for new fields)
                - enum_restrict: bool - enum restriction (for new fields)
        """
        # For nested updates, validate the base key is a container type
        if is_nested:
            container_types = {"dict", "list", "tuple", "set"}
            ft_info = field_types.get(field_key)
            expected_type = None
            if isinstance(ft_info, dict):
                expected_type = (
                    ft_info.get("field_type") or ft_info.get("type") or "Any"
                )

            # explicit_types override if provided
            if explicit_types_dict and field_key in explicit_types_dict:
                spec = explicit_types_dict[field_key]
                if isinstance(spec, dict):
                    expected_type = spec.get("type") or expected_type
                elif isinstance(spec, str):
                    expected_type = spec

            from orchestra.web.api.log.utils.type_utils import types_match

            if expected_type and not any(
                types_match(x, expected_type) for x in container_types
            ):
                raise ValueError(
                    f"Type mismatch for field '{field_key}': "
                    f"field has strict type '{expected_type}', but nested path was provided. Expected type: {expected_type}, Field type: {ft_info.get('field_type', 'Any')}",
                )

        # Check if field exists
        if field_key in field_types:
            # Field exists - caller should enforce types separately
            return {"exists": True}
        else:
            # Field doesn't exist - prepare metadata for creation
            mutable = (
                explicit_types_dict.get(field_key, {}).get("mutable", False)
                if explicit_types_dict
                else False
            )
            unique = (
                explicit_types_dict.get(field_key, {}).get("unique", False)
                if explicit_types_dict
                else False
            )

            # Check for explicit type
            field_type = None
            enum_values = None
            enum_restrict = False
            if explicit_types_dict and field_key in explicit_types_dict:
                field_spec = explicit_types_dict[field_key]
                if isinstance(field_spec, dict):
                    field_type = field_spec.get("type")
                    enum_values = field_spec.get("values")
                    enum_restrict = field_spec.get("restrict", False)
                elif isinstance(field_spec, str):
                    field_type = field_spec

            return {
                "exists": False,
                "mutable": mutable,
                "unique": unique,
                "field_type": field_type,
                "enum_values": enum_values,
                "enum_restrict": enum_restrict,
            }

    async def upload_image_to_bucket(self, image_base64: str) -> str:
        """Upload image to bucket and return the URL."""
        try:
            url, _ = self.bucket_service.upload_media(image_base64, "image/jpeg")
            return url
        except Exception as e:
            raise ValueError(f"Failed to upload image to bucket: {str(e)}")

    async def upload_audio_to_bucket(self, audio_base64: str) -> str:
        """Upload audio to bucket and return the URL."""
        try:
            url, _ = self.bucket_service.upload_media(audio_base64, "audio/mpeg")
            return url
        except Exception as e:
            raise ValueError(f"Failed to upload audio to bucket: {str(e)}")

    async def get_image_from_bucket(self, url: str) -> Optional[str]:
        """Retrieve image from bucket and return as base64."""
        try:
            # Extract filename from URL
            filename = url.split("/")[-1]
            base64_content = self.bucket_service.get_media(filename)
            return base64_content
        except Exception as e:
            raise ValueError(f"Failed to retrieve image from bucket: {str(e)}")

    async def get_audio_from_bucket(self, url: str) -> Optional[str]:
        """Retrieve audio from bucket and return as base64."""
        try:
            # Extract filename from URL
            filename = url.split("/")[-1]
            base64_content = self.bucket_service.get_media(filename)
            return base64_content
        except Exception as e:
            raise ValueError(f"Failed to retrieve audio from bucket: {str(e)}")

    @staticmethod
    async def detect_media_type(raw_v: str) -> Optional[str]:
        """
        Detect if a string contains base64-encoded media (image or audio).

        Args:
            raw_v: The string value to check

        Returns:
            "image" if valid image data detected
            "audio" if valid audio data detected
            None if not valid base64 media
        """
        content_to_check = raw_v

        # Handle data URI format: data:image/png;base64,<content>
        if raw_v.startswith("data:") and "," in raw_v:
            # Split at first comma to remove MIME header
            content_to_check = raw_v.split(",", 1)[1]

        try:
            # Decode base64 safely
            decoded = base64.b64decode(content_to_check, validate=True)
        except Exception:
            return None  # Not valid base64

        # Check magic bytes for images
        if decoded.startswith(b"\x89PNG"):
            return "image"
        elif decoded.startswith(b"\xff\xd8\xff"):
            return "image"
        elif decoded.startswith((b"GIF87a", b"GIF89a")):
            return "image"
        elif (
            decoded.startswith(b"RIFF")
            and len(decoded) >= 12
            and decoded[8:12] == b"WEBP"
        ):
            return "image"
        elif decoded.startswith(b"BM"):
            return "image"
        # Check magic bytes for audio
        elif decoded.startswith(b"ID3") or decoded.startswith(b"\xff\xfb"):
            return "audio"
        elif (
            decoded.startswith(b"RIFF")
            and len(decoded) >= 12
            and decoded[8:12] == b"WAVE"
        ):
            return "audio"
        elif decoded.startswith(b"fLaC"):
            return "audio"
        elif decoded.startswith(b"OggS"):
            return "audio"

        return None  # Unknown or unsupported

    @staticmethod
    async def infer_type(raw_k, raw_v, explicit_type=None):
        """
        Infer the type of a field value.

        Args:
            raw_k: The field name/key
            raw_v: The field value
            explicit_type: Optional user-specified type (string, dict, or Pydantic JSON schema)
                           - String types: "List[int]", "str", "enum", etc.
                           - Pydantic schemas: dict with JSON Schema structure
                           When provided, this overrides all heuristic type inference

        Returns:
            The inferred type as a string. If explicit_type is provided:
            - For Pydantic schemas: returns a simple inferred type (e.g., "list", "dict")
            - For string types: returns the normalized type string (e.g., "List[int]")
            Otherwise, infers a normalized possibly-nested type from the value.

        Note:
            Priority (highest to lowest):
            1. Explicit type (if provided) - with Pydantic validation if it's a schema
            2. Recursive value-based inference (containers allowed heterogeneous element types)
            3. String special forms (datetime/date/time/timedelta) and media via magic bytes
        """
        from orchestra.web.api.log.utils.type_utils import (
            infer_type_from_value,
            is_pydantic_schema,
            normalize_pydantic_schema,
            normalize_type_string,
            pydantic_schema_to_string,
            validate_value_against_pydantic_schema,
        )

        if explicit_type is not None:
            # Check if it's a Pydantic JSON schema (dict or JSON string)
            if is_pydantic_schema(explicit_type):
                try:
                    # Validate the value against the JSON schema using jsonschema library
                    is_valid, error_msg = validate_value_against_pydantic_schema(
                        raw_v,
                        explicit_type,
                    )
                    if not is_valid:
                        raise ValueError(
                            f"Value does not match Pydantic schema for field '{raw_k}': {error_msg}",
                        )

                    # Normalize to schema dict (if it's a JSON string, parse it)
                    schema = normalize_pydantic_schema(explicit_type)
                    # Store the full schema JSON string in inferred_type
                    return pydantic_schema_to_string(schema)

                except ValueError as e:
                    # Re-raise validation errors
                    raise e
                except Exception as e:
                    # Handle other errors gracefully
                    raise ValueError(
                        f"Error processing Pydantic schema for field '{raw_k}': {str(e)}",
                    )

            # Regular string type
            return normalize_type_string(explicit_type)

        # Delegate to type_utils and pass media detector hook
        return infer_type_from_value(
            raw_v,
            media_detector=getattr(LogDAO, "detect_media_type", None),
        )

    async def get_ids_by_filter(
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
        from orchestra.settings import settings

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

        # JSONB Mode: Filter directly on LogEvent.data JSONB column
        if settings.use_jsonb_queries:
            for key, value in filters.items():
                # Use JSONB containment operator @> or path-based filtering
                # LogEvent.data @> '{"key": value}'::jsonb
                query = query.where(
                    LogEvent.data[key].astext == str(value)
                    if isinstance(value, str)
                    else LogEvent.data[key] == cast(literal(value), JSONB),
                )
            result = await self.session.execute(query)
            return [row[0] for row in result]

        # EAV Mode: Use Log/LogEventLog tables
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
        result = await self.session.execute(query)
        return [row[0] for row in result]

    async def filter(
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
        async def normalize_input(value):
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
        rows = await self.session.execute(query)
        if defer:
            return rows
        return rows.fetchall()

    async def rename_field_in_logs(
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

            log_event_ids = [
                row[0] for row in await self.session.execute(log_event_query)
            ]

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

            # JSONB Mode: Update LogEvent.data JSONB column - rename key within the JSON object
            # Only executed when JSONB mode is enabled to maintain EAV/JSONB separation
            from orchestra.settings import settings

            if settings.use_jsonb_queries and log_event_ids:
                from sqlalchemy import text

                # Use raw SQL for JSONB key rename since SQLAlchemy doesn't have
                # direct support for this operation
                # Uses PostgreSQL JSONB operators: remove old key (-), add new key with same value (||)
                # This is a single bulk UPDATE - O(1) query regardless of number of log events
                await self.session.execute(
                    text(
                        """
                        UPDATE log_event
                        SET data = (data - :old_key) || jsonb_build_object(:new_key, data->:old_key)
                        WHERE id = ANY(:log_event_ids)
                        AND data ? :old_key
                    """,
                    ),
                    {
                        "old_key": old_field_name,
                        "new_key": new_field_name,
                        "log_event_ids": log_event_ids,
                    },
                )

            await self.session.commit()

        except Exception as e:
            await self.session.rollback()
            raise ValueError(f"Failed to rename field: {str(e)}")

    async def _bulk_delete_gcs_media(self, logs_query: Query):
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

    async def _bulk_delete_gcs_media_jsonb(
        self,
        log_event_ids: List[int],
        project_id: int,
        field_names: Optional[List[str]] = None,
    ):
        """
        Finds all image/audio fields in LogEvent.data for the given log_event_ids
        and deletes the corresponding files from GCS.

        This is the JSONB equivalent of _bulk_delete_gcs_media, used when
        settings.use_jsonb_queries is enabled.

        Args:
            log_event_ids: List of LogEvent IDs to check for media fields
            project_id: The project ID to query FieldTypes for
            field_names: Optional list of field names to check. If None, checks all
                        media fields in the project.
        """
        if not log_event_ids:
            return

        gcs_url_prefix = (
            f"https://storage.googleapis.com/{self.bucket_service.bucket_name}/"
        )

        # Query FieldType for media fields (field_type IN ('image', 'audio'))
        media_field_query = self.session.query(FieldType.field_name).filter(
            FieldType.project_id == project_id,
            FieldType.field_type.in_(("image", "audio")),
        )

        # Filter to only specified field names if provided
        if field_names:
            media_field_query = media_field_query.filter(
                FieldType.field_name.in_(field_names),
            )

        media_fields = [row[0] for row in media_field_query.all()]

        if not media_fields:
            return

        logging.info(
            f"Found {len(media_fields)} media field(s) to check for GCS deletion in JSONB mode.",
        )

        # Query LogEvent.data for the media field values
        log_events = (
            self.session.query(LogEvent.id, LogEvent.data)
            .filter(
                LogEvent.id.in_(log_event_ids),
                LogEvent.project_id == project_id,
            )
            .all()
        )

        # Extract GCS URLs from the data JSONB column
        urls_to_delete = []
        for log_event_id, data in log_events:
            if not data:
                continue
            for field_name in media_fields:
                value = data.get(field_name)
                if isinstance(value, str):
                    # Strip potential quotes from JSONB string literal
                    clean_value = value.strip("\"'")
                    if clean_value.startswith(gcs_url_prefix):
                        urls_to_delete.append((log_event_id, field_name, clean_value))

        if not urls_to_delete:
            return

        logging.info(
            f"Found {len(urls_to_delete)} GCS URL(s) to delete from LogEvent.data.",
        )

        # Delete each file from GCS
        for log_event_id, field_name, url in urls_to_delete:
            try:
                filename = url.split("/")[-1]
                logging.warning(
                    f"Deleting GCS file: {filename} for log_event_id: {log_event_id}, field: {field_name}",
                )
                self.bucket_service.delete_media(filename)
            except Exception as e:
                # Log the error but don't stop the overall delete process
                logging.error(
                    f"Failed to delete GCS file for log_event_id {log_event_id}, field {field_name}: {str(e)}",
                )

    async def delete(self, id: int):
        """Deletes a single Log record and its associated GCS file if applicable."""
        try:
            log_query = select(Log).filter_by(id=id)
            log = log_query.one_or_none()

            if not log:
                raise ValueError(f"Log with id {id} not found.")

            # Call the bulk helper to handle GCS deletion
            self._bulk_delete_gcs_media(log_query)

            # Delete corresponding JSONLog if it exists
            # First get the log_event_id from LogEventLog association
            log_event_log = (
                (
                    await self.session.execute(
                        select(LogEventLog).filter_by(log_id=log.id),
                    )
                )
                .scalars()
                .first()
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
                await self.session.delete(json_log)

            # Delete the log record itself
            await self.session.delete(log)
            await self.session.commit()

        except Exception as e:
            await self.session.rollback()
            raise ValueError(f"Failed to delete log with id {id}: {e}")

    async def _check_uniqueness(self, entries: List[Dict[str, Any]]):
        unique_field_defs = {}  # (project_id, context_id, key) -> FieldType

        # Collect all project and context IDs from entries
        all_project_ids = set(e["project_id"] for e in entries if "project_id" in e)
        all_context_ids = set(e["context_id"] for e in entries if e.get("context_id"))

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

        # Fetch all contexts to check for composite keys
        contexts_with_composite_keys = {}
        if all_context_ids:
            contexts = (
                self.session.query(Context)
                .filter(Context.id.in_(all_context_ids))
                .all()
            )
            for ctx in contexts:
                if ctx.unique_keys and len(ctx.unique_keys) > 0:
                    contexts_with_composite_keys[ctx.id] = ctx

        # Group entries by project and context
        grouped_by_context = defaultdict(list)
        for entry in entries:
            context_id = entry.get("context_id")
            # Include entry if it has a unique field OR if its context has composite keys
            if (
                entry.get("project_id"),
                context_id,
                entry.get("key"),
            ) in unique_field_defs or context_id in contexts_with_composite_keys:
                grouped_by_context[(entry.get("project_id"), context_id)].append(entry)

        if not grouped_by_context:
            return

        # Check for duplicates within the batch first
        batch_unique_values = defaultdict(set)
        for (project_id, context_id), context_entries in grouped_by_context.items():
            context = (
                (await self.session.execute(select(Context).filter_by(id=context_id)))
                .scalars()
                .one_or_none()
                if context_id
                else None
            )
            composite_keys = (
                context.unique_keys
                if context and context.unique_keys and len(context.unique_keys) > 1
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
                    # Only check if this field is marked as unique
                    if (
                        entry.get("project_id"),
                        context_id,
                        entry["key"],
                    ) in unique_field_defs:
                        simple_val = entry["value"]
                        if (
                            simple_val
                            in batch_unique_values[
                                (project_id, context_id, entry["key"])
                            ]
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
                (await self.session.execute(select(Context).filter_by(id=context_id)))
                .scalars()
                .one_or_none()
                if context_id
                else None
            )
            composite_keys = (
                context.unique_keys
                if context and context.unique_keys and len(context.unique_keys) > 1
                else None
            )

            if composite_keys:
                # Handle composite key check - BATCH OPTIMIZED
                # Collect all complete composite key combinations from this batch
                log_events_to_check = defaultdict(dict)
                for entry in context_entries:
                    if entry["key"] in composite_keys:
                        log_events_to_check[entry["log_event_id"]][
                            entry["key"]
                        ] = entry["value"]

                # Filter to only complete composite key sets
                complete_key_values = [
                    (log_event_id, key_values)
                    for log_event_id, key_values in log_events_to_check.items()
                    if len(key_values) == len(composite_keys)
                ]

                if complete_key_values:
                    # Build a single batch query to check ALL composite key combinations
                    # Use OR of AND conditions for each composite key combination
                    all_conditions = []
                    for _, key_values in complete_key_values:
                        # Each composite key set needs ALL its key-value pairs to match
                        key_conditions = [
                            and_(
                                Log.key == k,
                                Log.value == literal(v, type_=JSONB),
                            )
                            for k, v in key_values.items()
                        ]
                        all_conditions.extend(key_conditions)

                    # Single query to find any log_events with matching composite keys
                    q = (
                        select(LogEventLog.log_event_id)
                        .join(Log, Log.id == LogEventLog.log_id)
                        .join(
                            LogEventContext,
                            LogEventContext.log_event_id == LogEventLog.log_event_id,
                        )
                        .where(LogEventContext.context_id == context_id)
                        .where(or_(*all_conditions))
                        .group_by(LogEventLog.log_event_id)
                        .having(func.count(Log.id) >= len(composite_keys))
                    )

                    # Get all potentially matching log_event_ids
                    potential_matches = [
                        row[0] for row in await self.session.execute(q).fetchall()
                    ]

                    if potential_matches:
                        # For each potential match, verify it's an exact composite key match
                        # by checking if it has exactly the same key-value pairs
                        for _, key_values in complete_key_values:
                            # Check if this specific composite key combination exists
                            verify_q = (
                                select(LogEventLog.log_event_id)
                                .join(Log, Log.id == LogEventLog.log_id)
                                .join(
                                    LogEventContext,
                                    LogEventContext.log_event_id
                                    == LogEventLog.log_event_id,
                                )
                                .where(LogEventContext.context_id == context_id)
                                .where(LogEventLog.log_event_id.in_(potential_matches))
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

                            if await self.session.execute(verify_q.limit(1)).first():
                                raise ValueError(
                                    f"Duplicate entry for composite key {key_values}.",
                                )
            else:
                # Handle simple unique key check (original logic, but scoped to this context group)
                keys_and_values = defaultdict(list)
                for entry in context_entries:
                    # Only collect values for fields that are marked as unique
                    if (
                        entry.get("project_id"),
                        context_id,
                        entry["key"],
                    ) in unique_field_defs:
                        keys_and_values[entry["key"]].append(entry["value"])

                for key, values in keys_and_values.items():
                    # Check in Log table (EAV mode)
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

                    if await self.session.execute(q.limit(1)).first():
                        raise ValueError(f"Duplicate entry for unique field '{key}'.")

                    # JSONB Mode: Also check in LogEvent.data JSONB column
                    # Only executed when JSONB mode is enabled to maintain EAV/JSONB separation
                    from orchestra.settings import settings

                    if settings.use_jsonb_queries and values:
                        import json as json_module

                        from sqlalchemy import text as sql_text

                        # BATCH OPTIMIZED: Build a single query to check all values at once
                        # Uses JSONB @> operator with OR conditions for each value
                        # This is O(1) query instead of O(N) queries
                        or_conditions = " OR ".join(
                            f"le.data @> CAST(:value_{i} AS jsonb)"
                            for i in range(len(values))
                        )

                        if context_id is not None:
                            jsonb_query = f"""
                                SELECT le.id FROM log_event le
                                JOIN log_event_context lec ON le.id = lec.log_event_id
                                WHERE le.project_id = :project_id
                                AND lec.context_id = :context_id
                                AND ({or_conditions})
                                LIMIT 1
                            """
                            params = {
                                "project_id": project_id,
                                "context_id": context_id,
                            }
                        else:
                            jsonb_query = f"""
                                SELECT le.id FROM log_event le
                                WHERE le.project_id = :project_id
                                AND ({or_conditions})
                                LIMIT 1
                            """
                            params = {"project_id": project_id}

                        # Add value parameters
                        for i, value in enumerate(values):
                            params[f"value_{i}"] = json_module.dumps({key: value})

                        jsonb_result = await self.session.execute(
                            sql_text(jsonb_query),
                            params,
                        ).first()

                        if jsonb_result:
                            raise ValueError(
                                f"Duplicate entry for unique field '{key}'.",
                            )

    async def bulk_create(
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

                # Determine inferred type for Log.inferred_type column
                # Priority: explicit type > infer from value
                # If explicit type is a Pydantic JSON schema, validate using jsonschema path
                if inferred_type is not None:
                    try:
                        from orchestra.web.api.log.utils.type_utils import (
                            is_pydantic_schema,
                            normalize_pydantic_schema,
                            pydantic_schema_to_string,
                            validate_value_against_pydantic_schema,
                        )

                        if is_pydantic_schema(inferred_type):
                            (
                                is_valid,
                                error_msg,
                            ) = validate_value_against_pydantic_schema(
                                value,
                                inferred_type,
                            )
                            if not is_valid:
                                raise ValueError(
                                    f"Value does not match Pydantic schema for field '{key}': {error_msg}",
                                )
                            # Store the full schema JSON string as the inferred_type
                            schema = normalize_pydantic_schema(inferred_type)
                            inferred_type = pydantic_schema_to_string(schema)
                        else:
                            # ensure string type
                            inferred_type = str(inferred_type)
                    except Exception as e:
                        raise e

                if inferred_type == "enum" and project_id is not None:
                    # Handle enum field type
                    enum_values = key_explicit_type.get("values")
                    enum_restrict = key_explicit_type.get("restrict", False)

                    try:
                        self._handle_enum_field_type(
                            project_id=project_id,
                            context_id=context_id,
                            key=key,
                            value=value,
                            enum_values=enum_values,
                            enum_restrict=enum_restrict,
                        )
                        # Enum values are strings - use "str" for Log.inferred_type
                        inferred_type = "str"
                    except ValueError as e:
                        raise e
                elif inferred_type is None:
                    # No explicit type - infer from value using clean inference logic
                    inferred_type = self.infer_type(key, value)

                # Handle media uploads
                # If infer_type detected it as image/audio, it's valid base64 with magic bytes
                # Just upload it - infer_type already validated it properly
                if inferred_type == "image" and isinstance(value, str):
                    value = self.upload_image_to_bucket(value)
                elif inferred_type == "audio" and isinstance(value, str):
                    value = self.upload_audio_to_bucket(value)
                if inferred_type == "datetime" and isinstance(value, str):
                    from orchestra.web.api.log.utils.type_utils import (
                        normalize_timestamp,
                    )

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
                result = await self.session.execute(stmt)
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
                    await self.session.execute(stmt_assoc)

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
                result_v = await self.session.execute(stmt_v)
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
                    await self.session.execute(stmt_v_assoc)

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
                result_json = await self.session.execute(stmt_json)
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
                    await self.session.execute(stmt_json_assoc)

            await self.session.flush()

        except Exception as e:
            raise e

    async def get_next_row_ids(
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
        await self.session.execute(insert_stmt)

        # Atomically increment the counter by the batch size and return the new value.
        # The database ensures this operation is safe from race conditions.
        result = await self.session.execute(
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
            await self.session.rollback()
            raise ValueError(
                f"Failed to get next batch of row IDs for parameter {param_key}",
            )

        # Calculate the range of IDs reserved in this batch
        start_id = new_max_id - count + 1
        return list(range(start_id, new_max_id + 1))

    async def get_next_param_version(
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
        await self.session.execute(insert_stmt)

        # Atomically update the row and get new version number
        result = await self.session.execute(
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

    async def _check_composite_key_exists(
        self,
        context_id: int,
        composite_values: Dict[str, Any],
    ) -> bool:
        """
        Check if a composite key combination already exists in the context.
        """
        q = (
            self.session.query(LogEvent.id)
            .join(LogEventContext)
            .filter(LogEventContext.context_id == context_id)
        )

        # Add filters for each composite key column
        for col_name, col_value in composite_values.items():
            q = q.join(LogEventLog, LogEventLog.log_event_id == LogEvent.id).join(
                Log,
                and_(
                    Log.id == LogEventLog.log_id,
                    Log.key == col_name,
                    Log.value == col_value,
                ),
            )

        # Ensure we have exactly the composite key columns
        q = q.group_by(LogEvent.id).having(func.count(Log.id) == len(composite_values))

        return await self.session.execute(q.limit(1)).first() is not None

    async def _validate_parent_exists(
        self,
        context_id: int,
        parent_ids: Dict[str, Any],
    ) -> bool:
        """
        Validates that a log with the given parent ID combination already exists in the context.

        Note: For batch operations, use `_batch_validate_parents` to avoid N+1 queries.
        This method is kept for single-item validation or backward compatibility.
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
        return await self.session.execute(q.limit(1)).first() is not None

    async def _batch_validate_parents(
        self,
        context_id: int,
        parent_combinations: List[Dict[str, Any]],
    ) -> set:
        """
        Validate multiple parent combinations in a single query.

        Args:
            context_id: The context ID to check within
            parent_combinations: List of parent key-value dicts to validate

        Returns:
            Set of valid parent combinations as frozensets of (key, value) tuples.
            Each frozenset represents a parent combination that exists in the context.
        """
        if not parent_combinations:
            return set()

        # Deduplicate parent combinations
        unique_parents = []
        seen = set()
        for parent_dict in parent_combinations:
            frozen = frozenset(parent_dict.items())
            if frozen not in seen:
                seen.add(frozen)
                unique_parents.append(parent_dict)

        if not unique_parents:
            return set()

        # Build OR conditions for all unique parent combinations
        all_conditions = []
        for parent_dict in unique_parents:
            for k, v in parent_dict.items():
                all_conditions.append(
                    and_(Log.key == k, Log.value == literal(v, type_=JSONB)),
                )

        # Single query to find all log_events that have ANY of these key-value pairs
        q = (
            select(LogEventLog.log_event_id, Log.key, Log.value)
            .join(Log, Log.id == LogEventLog.log_id)
            .join(
                LogEventContext,
                LogEventContext.log_event_id == LogEventLog.log_event_id,
            )
            .where(LogEventContext.context_id == context_id)
            .where(or_(*all_conditions))
        )

        results = await self.session.execute(q).fetchall()

        # Group results by log_event_id
        log_event_keys: Dict[int, Dict[str, Any]] = defaultdict(dict)
        for log_event_id, key, value in results:
            log_event_keys[log_event_id][key] = value

        # Check which parent combinations are fully satisfied
        valid_parents = set()
        for parent_dict in unique_parents:
            parent_frozen = frozenset(parent_dict.items())
            # Check if any log_event has all the required key-value pairs
            for le_id, le_keys in log_event_keys.items():
                if all(
                    k in le_keys and le_keys[k] == v for k, v in parent_dict.items()
                ):
                    valid_parents.add(parent_frozen)
                    break

        return valid_parents

    async def get_next_composite_ids(
        self,
        project_id: int,
        context_id: int,
        unique_keys: Dict[str, str],
        provided_values: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Generates composite key values, handling both counting and non-counting columns.
        For counting columns, generates auto-incrementing values respecting hierarchy.
        For non-counting columns, uses the provided values directly.

        OPTIMIZED: Uses batch operations for parent validation and ID reservation
        to achieve O(K) queries where K = unique param_key patterns, instead of
        O(N × C) queries where N = batch size and C = counting columns per entry.

        Note: For hierarchical counters where parent is also auto-generated,
        we process those sequentially as the param_key depends on the parent's
        auto-generated value.
        """
        if not provided_values:
            return []

        # Get the context to access the ordered columns and auto_counting config
        context = (
            (await self.session.execute(select(Context).filter_by(id=context_id)))
            .scalars()
            .one()
        )

        # Get auto-counting configuration
        auto_counting = context.auto_counting or {}

        # Get all columns: unique_keys + any auto-counting columns not in unique_keys
        unique_key_columns = context.unique_key_names or list(unique_keys.keys())
        all_auto_counting_columns = list(auto_counting.keys())

        # Combine unique key columns and auto-counting columns
        all_columns = unique_key_columns[:]
        for col in all_auto_counting_columns:
            if col not in all_columns:
                all_columns.append(col)

        # Determine which columns are counting based on auto_counting config
        counting_columns = [k for k in all_columns if k in auto_counting]
        non_counting_columns = [k for k in all_columns if k not in auto_counting]

        # =====================================================================
        # PHASE 1: PRE-COLLECT PARENT VALIDATIONS AND CATEGORIZE COLUMNS
        # Identify which columns can be batch-reserved vs need sequential processing
        # =====================================================================
        parents_to_validate: List[Dict[str, Any]] = []

        # Categorize counting columns:
        # - "independent": no parent (auto_counting[col] is None)
        # - "provided_parent": parent value is explicitly provided in the request
        # - "auto_parent": parent is also auto-generated (needs sequential processing)
        column_categories: Dict[str, str] = {}
        for col_name in counting_columns:
            parent_col = auto_counting.get(col_name)
            if parent_col is None:
                column_categories[col_name] = "independent"
            else:
                column_categories[col_name] = "hierarchical"

        # Pre-collect data for batch operations
        # For independent counters, we can count how many IDs we need
        independent_counts: Dict[str, int] = defaultdict(int)
        # For counters with PROVIDED parents, collect param_keys
        provided_parent_counts: Dict[str, int] = defaultdict(int)
        # Store entry metadata for processing
        entry_metadata: List[Dict[str, Any]] = []

        for provided_value in provided_values:
            provided_counting = {
                k: v for k, v in provided_value.items() if k in counting_columns
            }

            entry_info = {
                "provided_value": provided_value,
                "provided_counting": provided_counting,
                "independent_keys": [],  # (col_name, param_key) for independent counters
                "provided_parent_keys": [],  # (col_name, param_key) for provided parents
                "auto_parent_cols": [],  # col_names needing sequential processing
                "parent_validations": [],
            }

            for col_name in counting_columns:
                if col_name in provided_counting:
                    continue  # Value provided, no auto-increment needed

                parent_col = auto_counting.get(col_name)

                if parent_col is None:
                    # Independent counter
                    param_key = col_name
                    entry_info["independent_keys"].append((col_name, param_key))
                    independent_counts[param_key] += 1
                else:
                    # Hierarchical counter - check if parent is provided or auto
                    # Walk up hierarchy to see if all ancestors are provided
                    can_precompute = True
                    param_key_parts = []
                    current_col = col_name

                    while current_col in auto_counting:
                        parent_of_current = auto_counting[current_col]
                        if parent_of_current is None:
                            break

                        if parent_of_current in provided_counting:
                            # Parent is provided
                            parent_value = provided_counting[parent_of_current]
                            param_key_parts.insert(
                                0,
                                f"{parent_of_current}={parent_value}",
                            )

                            # Collect for parent validation
                            if current_col == col_name:
                                parent_check = {parent_of_current: parent_value}
                                entry_info["parent_validations"].append(parent_check)
                                parents_to_validate.append(parent_check)
                        elif parent_of_current not in counting_columns:
                            # Parent is a non-counting column that should be provided
                            if parent_of_current in provided_value:
                                parent_value = provided_value[parent_of_current]
                                param_key_parts.insert(
                                    0,
                                    f"{parent_of_current}={parent_value}",
                                )
                            else:
                                raise ValueError(
                                    f"Parent column '{parent_of_current}' value must be provided for '{current_col}'",
                                )
                        else:
                            # Parent is also a counting column needing auto-increment
                            can_precompute = False
                            break

                        current_col = parent_of_current

                    if can_precompute:
                        param_key_parts.append(col_name)
                        param_key = "::".join(param_key_parts)
                        entry_info["provided_parent_keys"].append((col_name, param_key))
                        provided_parent_counts[param_key] += 1
                    else:
                        # Needs sequential processing
                        entry_info["auto_parent_cols"].append(col_name)

            entry_metadata.append(entry_info)

        # =====================================================================
        # PHASE 2: BATCH VALIDATE PARENTS (single query)
        # =====================================================================
        if parents_to_validate:
            valid_parents = self._batch_validate_parents(
                context_id,
                parents_to_validate,
            )

            for parent_dict in parents_to_validate:
                parent_frozen = frozenset(parent_dict.items())
                if parent_frozen not in valid_parents:
                    k, v = next(iter(parent_dict.items()))
                    raise ValueError(
                        f"Parent ID {k}={v} does not exist in this context.",
                    )

        # =====================================================================
        # PHASE 3: BATCH RESERVE IDS FOR INDEPENDENT AND PROVIDED-PARENT COLUMNS
        # =====================================================================
        reserved_ids: Dict[str, List[int]] = {}
        id_consumption_index: Dict[str, int] = defaultdict(int)

        # Reserve for independent counters
        for param_key, count in independent_counts.items():
            reserved_ids[param_key] = self.get_next_row_ids(
                project_id=project_id,
                context_id=context_id,
                param_key=param_key,
                count=count,
            )

        # Reserve for provided-parent counters
        for param_key, count in provided_parent_counts.items():
            reserved_ids[param_key] = self.get_next_row_ids(
                project_id=project_id,
                context_id=context_id,
                param_key=param_key,
                count=count,
            )

        # =====================================================================
        # PHASE 4: PROCESS ENTRIES
        # Use pre-reserved IDs where possible, sequential for auto-parent cols
        # =====================================================================
        completed_ids = []

        for entry_info in entry_metadata:
            provided_value = entry_info["provided_value"]
            provided_counting = entry_info["provided_counting"]
            final_values = {}

            # Copy all non-counting values directly
            for col_name in non_counting_columns:
                if col_name in provided_value:
                    final_values[col_name] = provided_value[col_name]

            # Handle counting columns
            if counting_columns:
                # First: Use pre-reserved IDs for independent counters
                for col_name, param_key in entry_info["independent_keys"]:
                    idx = id_consumption_index[param_key]
                    next_id = reserved_ids[param_key][idx]
                    id_consumption_index[param_key] += 1
                    final_values[col_name] = next_id

                # Second: Use pre-reserved IDs for provided-parent counters
                for col_name, param_key in entry_info["provided_parent_keys"]:
                    idx = id_consumption_index[param_key]
                    next_id = reserved_ids[param_key][idx]
                    id_consumption_index[param_key] += 1
                    final_values[col_name] = next_id

                # Third: Process auto-parent columns sequentially
                for col_name in entry_info["auto_parent_cols"]:
                    # Build param_key using auto-generated parent values
                    param_key_parts = []
                    current_col = col_name

                    while current_col in auto_counting:
                        parent_of_current = auto_counting[current_col]
                        if parent_of_current is None:
                            break

                        # Get parent value from final_values or provided
                        if parent_of_current in provided_counting:
                            parent_value = provided_counting[parent_of_current]
                        elif parent_of_current in final_values:
                            parent_value = final_values[parent_of_current]
                        else:
                            raise ValueError(
                                f"Parent column '{parent_of_current}' value must be provided for '{current_col}'",
                            )

                        param_key_parts.insert(0, f"{parent_of_current}={parent_value}")
                        current_col = parent_of_current

                    param_key_parts.append(col_name)
                    param_key = "::".join(param_key_parts)

                    # Get next ID (this is sequential, but only for auto-parent cols)
                    next_id = self.get_next_row_ids(
                        project_id=project_id,
                        context_id=context_id,
                        param_key=param_key,
                        count=1,
                    )[0]
                    final_values[col_name] = next_id

                # Add all provided counting values
                final_values.update(provided_counting)

            # Ensure the final dict preserves the original order of unique_keys
            ordered_final_values = {}
            for key in all_columns:
                if key in final_values:
                    ordered_final_values[key] = final_values[key]

            completed_ids.append(ordered_final_values)

        return completed_ids

    async def bulk_update(
        self,
        updates: List[Dict[str, Any]],
        overwrite: bool = False,
        field_types: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Update multiple Log entries with partial success support.

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

        Returns:
            Dictionary with:
                - successful_update_ids: List of log_event_ids that were updated successfully
                - failed: List of dicts with log_event_id and error message
        """
        if not updates:
            return {"successful_update_ids": [], "failed": []}

        try:
            self._check_uniqueness(updates)
        except ValueError as e:
            detail = e.detail if isinstance(e, HTTPException) else str(e)
            return {
                "successful_update_ids": [],
                "failed": [
                    {
                        "error": f"Found differing log param value with the same version: {detail}",
                    },
                ],
            }
        except Exception as e:
            return {"successful_update_ids": [], "failed": [{"error": str(e)}]}

        field_types = field_types or {}

        # Group updates by log_event_id for partial success handling
        updates_by_log_id: Dict[int, List[Dict[str, Any]]] = {}
        for update_item in updates:
            le_id = update_item.get("log_event_id")
            if le_id:
                updates_by_log_id.setdefault(le_id, []).append(update_item)

        update_result = {"successful_update_ids": [], "failed": []}

        # Process each log_event_id independently
        for process_log_id, log_updates in updates_by_log_id.items():
            try:
                now = datetime.now(timezone.utc)

                # Group updates by log_event_id and key for efficient querying
                update_groups = {}
                for update_item in log_updates:
                    log_event_id = update_item.get("log_event_id")
                    key = update_item.get("key")
                    if not log_event_id or not key:
                        continue

                    group_key = (log_event_id, key)
                    update_groups[group_key] = update_item

                if not update_groups:
                    continue

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
                        (
                            await self.session.execute(
                                select(LogEvent).filter_by(id=log_event_id),
                            )
                        )
                        .scalars()
                        .first()
                    )
                    project_id = log_event.project_id if log_event else None

                    # Determine inferred type for Log.inferred_type column
                    # Priority: explicit type > infer from value
                    if inferred_type is not None:
                        try:
                            from orchestra.web.api.log.utils.type_utils import (
                                is_pydantic_schema,
                                normalize_pydantic_schema,
                                pydantic_schema_to_string,
                                validate_value_against_pydantic_schema,
                            )

                            if is_pydantic_schema(inferred_type):
                                (
                                    is_valid,
                                    error_msg,
                                ) = validate_value_against_pydantic_schema(
                                    value,
                                    inferred_type,
                                )
                                if not is_valid:
                                    raise ValueError(
                                        f"Value does not match Pydantic schema for field '{key}': {error_msg}",
                                    )
                                schema = normalize_pydantic_schema(inferred_type)
                                inferred_type = pydantic_schema_to_string(schema)
                            else:
                                inferred_type = str(inferred_type)
                        except Exception as e:
                            raise e

                    if inferred_type == "enum" and project_id is not None:
                        # Handle enum field type
                        enum_values = key_explicit_type.get("values")
                        enum_restrict = key_explicit_type.get("restrict", False)

                        try:
                            self._handle_enum_field_type(
                                project_id=project_id,
                                context_id=context_id,
                                key=key,
                                value=value,
                                enum_values=enum_values,
                                enum_restrict=enum_restrict,
                            )
                            # Enum values are strings - use "str" for Log.inferred_type
                            inferred_type = "str"
                        except ValueError as e:
                            raise e
                    elif inferred_type is None:
                        # No explicit type - infer from value using clean inference logic
                        inferred_type = self.infer_type(key, value)

                    # Handle media uploads
                    # If infer_type detected it as image/audio, it's valid base64 with magic bytes
                    # Just upload it - infer_type already validated it properly
                    json_value = value
                    if inferred_type == "image" and isinstance(value, str):
                        json_value = self.upload_image_to_bucket(value)
                    elif inferred_type == "audio" and isinstance(value, str):
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
                                LogEventJSONLog.log_event_id.in_(
                                    json_log_event_ids_check,
                                ),
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
                        result = await self.session.execute(stmt)
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
                            stmt_assoc = pg_insert(LogEventLog).values(
                                log_event_log_rows,
                            )
                            await self.session.execute(stmt_assoc)

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
                        result = await self.session.execute(stmt)
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
                            await self.session.execute(stmt_assoc)

                # Bulk update log event timestamps
                if log_event_ids_to_update:
                    stmt = (
                        update(LogEvent)
                        .where(LogEvent.id.in_(log_event_ids_to_update))
                        .values(updated_at=now)
                    )
                    await self.session.execute(stmt)

                # Mark this log_event_id as successful
                update_result["successful_update_ids"].append(process_log_id)

            except ValueError as e:
                detail = e.detail if isinstance(e, HTTPException) else str(e)
                update_result["failed"].append(
                    {
                        "log_event_id": process_log_id,
                        "error": f"Found differing log param value with the same version: {detail}",
                    },
                )
            except OverwriteError as e:
                detail = e.detail if isinstance(e, HTTPException) else str(e)
                update_result["failed"].append(
                    {
                        "log_event_id": process_log_id,
                        "error": f"Existing value cannot be overwritten because overwrite is set to False: {detail}",
                    },
                )
            except ImmutableFieldError as e:
                detail = e.detail if isinstance(e, HTTPException) else str(e)
                update_result["failed"].append(
                    {
                        "log_event_id": process_log_id,
                        "error": f"Field is immutable and cannot be modified: {detail}",
                    },
                )

        # Commit all successful updates
        await self.session.commit()
        return update_result

    async def _handle_enum_field_type(
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

        Note: For batch operations, use the batch enum handling implemented in
        `bulk_update_jsonb` (Steps 1-5) to avoid N+1 queries. This method
        executes a database query per call and should only be used for
        single-field operations (e.g., EAV mode, single updates).

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
                        await self.session.execute(stmt)

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

    async def _apply_patch_to_doc(self, doc, segments, new_value, overwrite=False):
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

    async def apply_jsonb_patch(
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
                        (
                            await self.session.execute(
                                select(Context).filter_by(id=context_id),
                            )
                        )
                        .scalars()
                        .first()
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

            await self.session.commit()

        except (OverwriteError, ImmutableFieldError):
            await self.session.rollback()
            raise
        except Exception as e:
            await self.session.rollback()
            raise ValueError(f"Failed to apply JSONB patch: {str(e)}")

    async def bulk_update_jsonb(
        self,
        updates: List[Dict[str, Any]],
        overwrite: bool = False,
        field_types: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Update multiple LogEvent.data JSONB fields with partial success support.

        Directly updates the LogEvent.data JSONB column using atomic
        `data || jsonb_build_object(...)` operations.

        Key features:
        - **Bulk Operations**: Fetches all LogEvents in a single query
        - **Race Condition Safe**: Uses PostgreSQL's atomic `||` operator for JSONB merge
        - **No param versioning**: Updates overwrite current values directly
        - **Partial Success**: Each log_event_id is handled independently

        Args:
            updates: List of dictionaries with the following keys:
                - log_event_id: int
                - key: str
                - value: Any
                - explicit_types: Dict (optional)
                - context_id: int (optional)
                - project_id: int (optional)
                - overwrite: bool (optional, per-update override)
            overwrite: Whether to allow overwriting existing values
            field_types: Dictionary of field types with mutable flags

        Returns:
            Dictionary with:
                - successful_update_ids: List of log_event_ids that were updated successfully
                - failed: List of dicts with log_event_id and error message
        """
        import json

        from sqlalchemy import text

        if not updates:
            return {"successful_update_ids": [], "failed": []}

        field_types = field_types or {}

        # Group updates by log_event_id for partial success handling
        updates_by_log_id: Dict[int, List[Dict[str, Any]]] = {}
        for update_item in updates:
            le_id = update_item.get("log_event_id")
            if le_id:
                updates_by_log_id.setdefault(le_id, []).append(update_item)

        update_result = {"successful_update_ids": [], "failed": []}

        if not updates_by_log_id:
            return update_result

        # =====================================================================
        # BULK FETCH: Get all LogEvents in a SINGLE query with FOR UPDATE to
        # prevent race conditions (pessimistic locking)
        # =====================================================================
        all_log_ids = list(updates_by_log_id.keys())
        log_events = (
            self.session.query(LogEvent)
            .filter(LogEvent.id.in_(all_log_ids))
            .with_for_update()  # Lock rows to prevent concurrent modifications
            .all()
        )

        # Build a lookup map for O(1) access
        log_event_map = {le.id: le for le in log_events}

        # Identify missing log_event_ids
        found_ids = set(log_event_map.keys())
        for log_id in all_log_ids:
            if log_id not in found_ids:
                update_result["failed"].append(
                    {
                        "log_event_id": log_id,
                        "error": f"LogEvent with id {log_id} not found",
                    },
                )

        now = datetime.now(timezone.utc)

        # =====================================================================
        # STEP 1: EXTRACT ENUM FIELDS FROM UPDATE BATCH
        # Scan through all updates to identify fields with inferred_type == "enum"
        # =====================================================================
        enum_field_info: Dict[tuple, Dict[str, Any]] = {}
        # Maps (project_id, context_id, field_name) -> {
        #   "values": set of enum values from batch,
        #   "enum_values": explicit enum_values list,
        #   "enum_restrict": restrict flag
        # }

        for process_log_id_scan, log_updates_scan in updates_by_log_id.items():
            if process_log_id_scan not in log_event_map:
                continue
            log_event_scan = log_event_map[process_log_id_scan]
            for update_data in log_updates_scan:
                key = update_data.get("key")
                value = update_data.get("value")
                explicit_types = update_data.get("explicit_types", {})
                key_explicit_type = explicit_types.get(key, {})
                context_id = update_data.get("context_id")
                project_id = update_data.get("project_id") or log_event_scan.project_id

                if not key or project_id is None:
                    continue

                # Determine inferred type
                inferred_type = key_explicit_type.get("type")
                if inferred_type is None:
                    inferred_type = self.infer_type(key, value)

                if inferred_type == "enum":
                    field_key = (project_id, context_id, key)
                    if field_key not in enum_field_info:
                        enum_field_info[field_key] = {
                            "values": set(),
                            "enum_values": key_explicit_type.get("values"),
                            "enum_restrict": key_explicit_type.get("restrict", False),
                        }
                    if isinstance(value, str):
                        enum_field_info[field_key]["values"].add(value)

        # =====================================================================
        # STEP 2: BATCH FETCH FIELDTYPE RECORDS FOR ENUM FIELDS
        # Execute a single SELECT query to fetch all relevant FieldType records
        # =====================================================================
        field_type_map: Dict[tuple, FieldType] = {}
        if enum_field_info:
            # Build OR conditions for each (project_id, context_id, field_name) tuple
            or_conditions = []
            for (proj_id, ctx_id, fld_name) in enum_field_info.keys():
                if ctx_id is None:
                    or_conditions.append(
                        and_(
                            FieldType.project_id == proj_id,
                            FieldType.context_id.is_(None),
                            FieldType.field_name == fld_name,
                        ),
                    )
                else:
                    or_conditions.append(
                        and_(
                            FieldType.project_id == proj_id,
                            FieldType.context_id == ctx_id,
                            FieldType.field_name == fld_name,
                        ),
                    )

            if or_conditions:
                existing_field_types = (
                    self.session.query(FieldType).filter(or_(*or_conditions)).all()
                )
                for ft in existing_field_types:
                    field_type_map[(ft.project_id, ft.context_id, ft.field_name)] = ft

        # =====================================================================
        # STEP 3: VALIDATE AND COLLECT ENUM EXPANSIONS
        # For each enum field, validate values and determine which need expansion
        # =====================================================================
        fields_to_expand: Dict[tuple, List[str]] = {}  # field_key -> new values to add
        fields_to_create: List[Dict[str, Any]] = []  # FieldType records to create
        restricted_enum_errors: Dict[tuple, str] = {}  # field_key -> error message

        for field_key, info in enum_field_info.items():
            proj_id, ctx_id, fld_name = field_key
            batch_values = info["values"]
            explicit_enum_values = info["enum_values"]
            enum_restrict = info["enum_restrict"]

            if field_key in field_type_map:
                # FieldType exists - validate and collect expansions
                ft = field_type_map[field_key]
                current_enum_values = set(ft.enum_values or [])

                # Check which values are new
                new_values = batch_values - current_enum_values

                if new_values:
                    if ft.enum_restrict:
                        # Restricted enum - record error for later
                        restricted_enum_errors[field_key] = (
                            f"Value(s) {sorted(new_values)} not in allowed enum values "
                            f"for field '{fld_name}': {sorted(current_enum_values)}"
                        )
                    else:
                        # Open enum - collect for batch expansion
                        # Also include any explicit enum_values that aren't in current
                        values_to_add = list(new_values)
                        if isinstance(explicit_enum_values, list):
                            for ev in explicit_enum_values:
                                if (
                                    ev not in current_enum_values
                                    and ev not in values_to_add
                                ):
                                    values_to_add.append(ev)
                        fields_to_expand[field_key] = values_to_add
            else:
                # FieldType doesn't exist - prepare for creation
                initial_values = []
                if isinstance(explicit_enum_values, list):
                    initial_values.extend(explicit_enum_values)
                # Add batch values to initial values
                for v in batch_values:
                    if v not in initial_values:
                        initial_values.append(v)

                fields_to_create.append(
                    {
                        "project_id": proj_id,
                        "context_id": ctx_id,
                        "field_name": fld_name,
                        "field_type": "enum",
                        "field_category": "entry",
                        "enum_values": initial_values,
                        "enum_restrict": enum_restrict,
                    },
                )

        # =====================================================================
        # STEP 4: BATCH UPDATE ENUM VALUES
        # Execute bulk UPDATE to expand enum values for all fields that need it
        # =====================================================================
        if fields_to_expand:
            for field_key, new_values in fields_to_expand.items():
                proj_id, ctx_id, fld_name = field_key
                if ctx_id is None:
                    stmt = (
                        update(FieldType)
                        .where(
                            FieldType.project_id == proj_id,
                            FieldType.context_id.is_(None),
                            FieldType.field_name == fld_name,
                        )
                        .values(
                            enum_values=FieldType.enum_values.concat(new_values),
                        )
                    )
                else:
                    stmt = (
                        update(FieldType)
                        .where(
                            FieldType.project_id == proj_id,
                            FieldType.context_id == ctx_id,
                            FieldType.field_name == fld_name,
                        )
                        .values(
                            enum_values=FieldType.enum_values.concat(new_values),
                        )
                    )
                await self.session.execute(stmt)

        # =====================================================================
        # STEP 5: BULK CREATE MISSING FIELDTYPE RECORDS
        # Use INSERT ON CONFLICT DO NOTHING for atomic creation
        # =====================================================================
        if fields_to_create:
            stmt = pg_insert(FieldType).values(fields_to_create)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["project_id", "field_name", "context_id"],
            )
            await self.session.execute(stmt)

        # =====================================================================
        # UNIQUENESS CHECK: Validate unique field constraints for JSONB mode
        # BATCH OPTIMIZED: Single query to check all unique field violations
        # =====================================================================
        unique_fields = {
            k: v
            for k, v in field_types.items()
            if isinstance(v, dict) and v.get("unique", False)
        }
        unique_violations: Dict[int, str] = {}  # log_event_id -> error message

        if unique_fields:
            import json as json_module

            from sqlalchemy import text as sql_text

            # Collect all unique field updates to check in batch
            # Structure: [(log_id, key, value, context_id, project_id), ...]
            unique_checks = []
            for check_log_id, log_updates in updates_by_log_id.items():
                if check_log_id not in log_event_map:
                    continue

                log_event = log_event_map[check_log_id]
                project_id = log_event.project_id

                for update_data in log_updates:
                    key = update_data.get("key")
                    value = update_data.get("value")
                    context_id = update_data.get("context_id")

                    if key in unique_fields and value is not None:
                        unique_checks.append(
                            (check_log_id, key, value, context_id, project_id),
                        )

            if unique_checks:
                # Group checks by (project_id, context_id) for efficient batching
                from collections import defaultdict

                checks_by_scope = defaultdict(list)
                for check_log_id, key, value, context_id, project_id in unique_checks:
                    checks_by_scope[(project_id, context_id)].append(
                        (check_log_id, key, value),
                    )

                # Execute one batch query per (project_id, context_id) scope
                for (project_id, context_id), scope_checks in checks_by_scope.items():
                    # Build OR conditions for all values in this scope
                    or_conditions = []
                    params = {"project_id": project_id}
                    if context_id is not None:
                        params["context_id"] = context_id

                    # Map value index to (log_id, key) for result processing
                    value_to_log_info = {}
                    for i, (check_log_id, key, value) in enumerate(scope_checks):
                        value_json = json_module.dumps({key: value})
                        params[f"value_{i}"] = value_json
                        params[f"exclude_id_{i}"] = check_log_id
                        or_conditions.append(
                            f"(le.data @> CAST(:value_{i} AS jsonb) AND le.id != :exclude_id_{i})",
                        )
                        value_to_log_info[i] = (check_log_id, key)

                    # Build the batch query
                    if context_id is not None:
                        # Query with context_id filter and return which condition matched
                        case_parts = [
                            f"WHEN le.data @> CAST(:value_{i} AS jsonb) AND le.id != :exclude_id_{i} THEN {i}"
                            for i in range(len(scope_checks))
                        ]
                        jsonb_query = f"""
                            SELECT DISTINCT CASE {' '.join(case_parts)} END AS match_idx
                            FROM log_event le
                            JOIN log_event_context lec ON le.id = lec.log_event_id
                            WHERE le.project_id = :project_id
                            AND lec.context_id = :context_id
                            AND ({' OR '.join(or_conditions)})
                        """
                    else:
                        case_parts = [
                            f"WHEN le.data @> CAST(:value_{i} AS jsonb) AND le.id != :exclude_id_{i} THEN {i}"
                            for i in range(len(scope_checks))
                        ]
                        jsonb_query = f"""
                            SELECT DISTINCT CASE {' '.join(case_parts)} END AS match_idx
                            FROM log_event le
                            WHERE le.project_id = :project_id
                            AND ({' OR '.join(or_conditions)})
                        """

                    # Execute batch query
                    results = await self.session.execute(
                        sql_text(jsonb_query),
                        params,
                    ).fetchall()

                    # Process results to identify violated log_ids
                    for (match_idx,) in results:
                        if match_idx is not None:
                            log_id, key = value_to_log_info[match_idx]
                            if log_id not in unique_violations:
                                unique_violations[
                                    log_id
                                ] = f"Duplicate entry for unique field '{key}'."

        # Mark violated logs as failed
        for log_id, error_msg in unique_violations.items():
            update_result["failed"].append(
                {
                    "log_event_id": log_id,
                    "error": error_msg,
                },
            )

        # =====================================================================
        # PROCESS UPDATES: Validate and prepare atomic updates
        # Collect all valid updates first, then execute single batch UPDATE
        # =====================================================================
        batch_updates: List[tuple] = []  # List of (log_id, json_data_str)

        for process_log_id, log_updates in updates_by_log_id.items():
            if process_log_id not in log_event_map:
                continue  # Already marked as failed above

            if process_log_id in unique_violations:
                continue  # Already marked as failed due to uniqueness violation

            try:
                log_event = log_event_map[process_log_id]
                current_data = dict(log_event.data or {})

                # Process each update for this log_event_id
                updates_to_apply = {}
                for update_data in log_updates:
                    key = update_data.get("key")
                    value = update_data.get("value")
                    explicit_types = update_data.get("explicit_types", {})
                    key_explicit_type = explicit_types.get(key, {})
                    context_id = update_data.get("context_id")
                    project_id = update_data.get("project_id") or log_event.project_id
                    per_update_overwrite = update_data.get("overwrite", overwrite)

                    if not key:
                        continue

                    # Check if field exists in current data
                    field_exists_in_data = key in current_data

                    # Check overwrite permission
                    if field_exists_in_data and not per_update_overwrite:
                        if current_data[key] != value:
                            raise OverwriteError(
                                f"Cannot overwrite existing value for key '{key}'",
                            )

                    # Check field mutability
                    if key in field_types and context_id is not None:
                        field_info = field_types.get(key)
                        if field_info and not field_info.get("mutable", False):
                            if field_exists_in_data:
                                raise ImmutableFieldError(
                                    f"Field '{key}' is immutable",
                                )

                    # Determine inferred type for media handling
                    inferred_type = key_explicit_type.get("type")
                    if inferred_type is None:
                        inferred_type = self.infer_type(key, value)

                    # Handle media uploads
                    json_value = value
                    if inferred_type == "image" and isinstance(value, str):
                        json_value = self.upload_image_to_bucket(value)
                    elif inferred_type == "audio" and isinstance(value, str):
                        json_value = self.upload_audio_to_bucket(value)

                    # Handle enum field type validation using pre-fetched data
                    # (Batch enum handling was done in Steps 1-5 above)
                    if inferred_type == "enum" and project_id is not None:
                        field_key = (project_id, context_id, key)
                        # Check if this field had a restricted enum violation
                        if field_key in restricted_enum_errors:
                            # Only raise if this specific value is the problem
                            if isinstance(value, str):
                                ft = field_type_map.get(field_key)
                                if ft and value not in (ft.enum_values or []):
                                    raise ValueError(restricted_enum_errors[field_key])

                    updates_to_apply[key] = json_value

                # Stage this update for batch execution
                if updates_to_apply:
                    update_json = json.dumps(updates_to_apply)
                    batch_updates.append((process_log_id, update_json))

                # Mark this log_event_id as successful
                update_result["successful_update_ids"].append(process_log_id)

            except OverwriteError as e:
                update_result["failed"].append(
                    {
                        "log_event_id": process_log_id,
                        "error": f"Existing value cannot be overwritten because overwrite is set to False: {str(e)}",
                    },
                )
            except ImmutableFieldError as e:
                update_result["failed"].append(
                    {
                        "log_event_id": process_log_id,
                        "error": f"Field is immutable and cannot be modified: {str(e)}",
                    },
                )
            except ValueError as e:
                update_result["failed"].append(
                    {
                        "log_event_id": process_log_id,
                        "error": str(e),
                    },
                )
            except Exception as e:
                update_result["failed"].append(
                    {
                        "log_event_id": process_log_id,
                        "error": f"Unexpected error: {str(e)}",
                    },
                )

        # =====================================================================
        # BATCH UPDATE: Execute single UPDATE with parameterized arrays for O(1) roundtrip
        # This issues one SQL statement regardless of batch size and safely handles
        # special characters in JSON payloads via parameter binding
        # =====================================================================
        if batch_updates:
            from orchestra.web.api.log.utils.logging_utils import extract_key_order

            ids_array = [log_id for log_id, _ in batch_updates]
            data_array = [json_str for _, json_str in batch_updates]

            # Extract key_order for new data to preserve nested dict ordering
            key_order_array = []
            for _, json_str in batch_updates:
                update_data = json.loads(json_str)
                new_key_order = extract_key_order(update_data)
                key_order_array.append(json.dumps(new_key_order))

            # Use unnest with parameterized arrays - safe from SQL injection
            # Also update key_order by merging with existing key_order to preserve
            # nested dict ordering for new fields added during update
            update_sql = text(
                """
                UPDATE log_event
                SET data = COALESCE(log_event.data, '{}'::jsonb) || v.new_data::jsonb,
                    key_order = COALESCE(log_event.key_order, '{}'::jsonb) || v.new_key_order::jsonb,
                    updated_at = :now
                FROM unnest(:ids, :data, :key_orders) AS v(id, new_data, new_key_order)
                WHERE log_event.id = v.id
            """,
            )
            await self.session.execute(
                update_sql,
                {
                    "now": now,
                    "ids": ids_array,
                    "data": data_array,
                    "key_orders": key_order_array,
                },
            )

        # Commit all successful updates
        await self.session.commit()
        return update_result

    async def apply_jsonb_patch_jsonb(
        self,
        patches: List[Dict[str, Any]],
        overwrite: bool = False,
        field_types: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Apply JSONB patches to nested paths within LogEvent.data.

        Directly updates nested paths within the LogEvent.data JSONB column
        using jsonb_set().

        Key features:
        - **Bulk Operations**: Fetches all LogEvents in a single query
        - **Race Condition Safe**: Uses FOR UPDATE locking and atomic jsonb_set()
        - No Log/JSONLog table operations
        - Updates LogEvent.data directly
        - Returns result dict with successful/failed IDs for partial success

        Args:
            patches: List of dictionaries with the following keys:
                - log_event_id: int
                - base_key: str
                - path_segments: str (e.g., ".field" or "[0]")
                - new_value: Any
                - context_id: int (optional)
                - overwrite: bool (optional)
                - explicit_types: Dict (optional)
            overwrite: Whether to allow overwriting existing values
            field_types: Dictionary of field types with mutable flags

        Returns:
            Dict with:
                - successful_update_ids: List[int] - IDs successfully updated
                - failed: List[Dict] - List of {"log_event_id": int, "error": str}
        """
        import json

        result = {
            "successful_update_ids": [],
            "failed": [],
        }

        if not patches:
            return result

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

            # =====================================================================
            # BULK FETCH: Get all LogEvents in a SINGLE query with FOR UPDATE
            # =====================================================================
            all_log_ids = list(set(le_id for (le_id, _) in grouped.keys()))
            log_events = (
                self.session.query(LogEvent)
                .filter(LogEvent.id.in_(all_log_ids))
                .with_for_update()  # Lock rows to prevent concurrent modifications
                .all()
            )

            # Build lookup map
            log_event_map = {le.id: le for le in log_events}

            # Check for missing LogEvents and mark as failed
            for le_id in all_log_ids:
                if le_id not in log_event_map:
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": f"LogEvent not found for log_event_id={le_id}",
                        },
                    )

            # =====================================================================
            # PROCESS PATCHES: Validate and prepare updates
            # Track successful/failed IDs per (log_event_id, base_key) group
            # =====================================================================
            batch_patch_updates: List[tuple] = []  # List of (log_id, json_data_str)
            processed_log_ids: set = set()
            failed_log_ids: set = set()

            for (le_id, base_key), group in grouped.items():
                if le_id not in log_event_map:
                    failed_log_ids.add(le_id)
                    continue  # Already marked as failed above

                try:
                    log_event = log_event_map[le_id]

                    # Check mutability
                    ft_info = field_types.get(base_key)
                    if ft_info and not ft_info.get("mutable", False):
                        raise ImmutableFieldError(f"Field '{base_key}' is immutable")

                    # Get current data from LogEvent
                    current_data = dict(log_event.data or {})

                    # Get the current document for this base_key
                    if base_key not in current_data:
                        # Initialize if not exists
                        current_data[base_key] = {}

                    current_doc = copy.deepcopy(current_data.get(base_key, {}))

                    # Apply each patch to the document
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
                                if token:
                                    segments.append(token)
                                i = j

                        # Apply the patch to the document
                        current_doc = self._apply_patch_to_doc(
                            current_doc,
                            segments,
                            new_value,
                            patch_overwrite,
                        )

                    # Stage this update for batch execution
                    update_json = json.dumps({base_key: current_doc})
                    batch_patch_updates.append((le_id, update_json))
                    processed_log_ids.add(le_id)

                except OverwriteError as e:
                    failed_log_ids.add(le_id)
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": f"Existing nested value cannot be overwritten: {str(e)}",
                        },
                    )
                except ImmutableFieldError as e:
                    failed_log_ids.add(le_id)
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": f"Field or nested path is immutable: {str(e)}",
                        },
                    )
                except (IndexError, Exception) as e:
                    failed_log_ids.add(le_id)
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": f"Error applying nested update: {str(e)}",
                        },
                    )

            # =============================================================
            # BATCH UPDATE: Execute single UPDATE with parameterized arrays
            # This issues one SQL statement regardless of batch size and safely
            # handles special characters in JSON payloads via parameter binding
            # =============================================================
            if batch_patch_updates:
                from orchestra.web.api.log.utils.logging_utils import extract_key_order

                ids_array = [log_id for log_id, _ in batch_patch_updates]
                data_array = [json_str for _, json_str in batch_patch_updates]

                # Extract key_order for new data to preserve nested dict ordering
                key_order_array = []
                for _, json_str in batch_patch_updates:
                    update_data = json.loads(json_str)
                    new_key_order = extract_key_order(update_data)
                    key_order_array.append(json.dumps(new_key_order))

                # Use unnest with parameterized arrays - safe from SQL injection
                # Also update key_order by merging with existing key_order
                update_sql = text(
                    """
                    UPDATE log_event
                    SET data = COALESCE(log_event.data, '{}'::jsonb) || v.new_data::jsonb,
                        key_order = COALESCE(log_event.key_order, '{}'::jsonb) || v.new_key_order::jsonb,
                        updated_at = :now
                    FROM unnest(:ids, :data, :key_orders) AS v(id, new_data, new_key_order)
                    WHERE log_event.id = v.id
                """,
                )
                await self.session.execute(
                    update_sql,
                    {
                        "now": now,
                        "ids": ids_array,
                        "data": data_array,
                        "key_orders": key_order_array,
                    },
                )

            await self.session.commit()

            # Populate successful IDs (processed but not failed)
            result["successful_update_ids"] = list(processed_log_ids - failed_log_ids)

        except Exception as e:
            await self.session.rollback()
            # On catastrophic failure, mark all as failed
            for le_id in all_log_ids:
                if le_id not in failed_log_ids:
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": f"Failed to apply JSONB patch: {str(e)}",
                        },
                    )

        return result
