import base64
import copy
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import Integer, and_, cast, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.models.orchestra_models import (
    Context,
    FieldType,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.services.bucket_service import BucketService

logger = logging.getLogger(__name__)


class OverwriteError(Exception):
    pass


class ImmutableFieldError(Exception):
    pass


def _transform_referenced_logs(equation: str, referenced_logs: Dict) -> List[Dict]:
    """
    Transform referenced_logs to use log placeholders (log0, log1, etc.) as keys.

    Args:
        equation: String containing placeholders like '{log0:a}+1 + {log1:b}'
        referenced_logs: Dict with original keys, e.g. {'a': 1, 'b': 2}

    Returns:
        Dict with transformed keys, e.g. {'log0': 1, 'log1': 2}
    """
    from orchestra.web.api.log.python2SQL import _extract_placeholders

    placeholders = _extract_placeholders(equation)  # ['log0:a', 'log1:a']

    transformed = {}
    for p in placeholders:
        log_key, original_key = p.split(":")  # 'log0:a' -> ('log0', 'a')
        transformed[log_key] = referenced_logs[original_key]

    return transformed


def _extract_field_names_from_equation(equation: str) -> List[str]:
    """
    Extract base field names from derived log equation for dependency tracking.
    """
    if not equation:
        return []

    try:
        from orchestra.web.api.log.python2SQL import _extract_placeholders

        placeholders = _extract_placeholders(equation)
        field_names = set()
        for p in placeholders:
            if ":" in p:
                field_name = p.split(":", 1)[1]
                field_names.add(field_name)
            else:
                logger.warning(f"Malformed placeholder '{p}' in equation: {equation}")
        return list(field_names)
    except Exception as e:
        logger.warning(f"Failed to extract field names from equation '{equation}': {e}")
        return []


class LogEventDAO:
    def __init__(self, session: Session, context_dao: Optional[ContextDAO] = None):
        self.session = session
        self.context_dao = context_dao or ContextDAO(session)
        self.bucket_service = BucketService()

    def bulk_create(
        self,
        project_id: int,
        count: int,
        context_id: Optional[int] = None,
        return_row_ids: bool = False,
        provided_unique_ids: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[List[int], tuple[List[int], List[Any]]]:
        """Create multiple LogEvent instances in one operation.

        Uses single INSERT with RETURNING clause to get all IDs in one database
        roundtrip instead of N separate ORM operations.
        """
        ts = datetime.now(timezone.utc)

        # Build list of row dictionaries for bulk INSERT
        # Both created_at and updated_at are set to same timestamp on creation
        # updated_at will be updated on subsequent modifications
        rows_to_insert = [
            {
                "project_id": project_id,
                "created_at": ts,
                "updated_at": ts,
                "data": {},  # Initialize empty JSONB
            }
            for _ in range(count)
        ]

        # Execute single INSERT with RETURNING to get all IDs in one statement
        stmt = pg_insert(LogEvent).values(rows_to_insert).returning(LogEvent.id)
        result = self.session.execute(stmt)
        log_event_ids = [row[0] for row in result.fetchall()]
        row_ids: List[Any] = [None] * count

        if context_id:
            # Associate logs with context
            associations = [
                LogEventContext(
                    log_event_id=log_event_id,
                    context_id=context_id,
                )
                for log_event_id in log_event_ids
            ]
            self.session.add_all(associations)

            # Check if this context needs composite unique keys or auto-counting
            context = self.session.query(Context).filter_by(id=context_id).one()
            if context.unique_keys or context.auto_counting:
                unique_keys = context.unique_keys

                # Generate composite key values
                if provided_unique_ids is None:
                    provided_unique_ids = [{} for _ in range(count)]

                try:
                    row_ids = self.get_next_composite_ids(
                        project_id=project_id,
                        context_id=context_id,
                        unique_keys=unique_keys or {},
                        provided_values=provided_unique_ids,
                    )
                except ValueError as e:
                    # Convert ValueError to a more user-friendly error
                    from fastapi import HTTPException

                    raise HTTPException(status_code=400, detail=str(e))

        self.session.flush()
        if return_row_ids:
            return (log_event_ids, row_ids)
        else:
            return log_event_ids

    def check_field_update(
        self,
        field_key: str,
        field_types: Dict[str, Any],
        explicit_types_dict: Dict[str, Any],
        is_nested: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Validate a field update for type compatibility and return field metadata.
        """
        if is_nested:
            container_types = {"dict", "list", "tuple", "set"}
            ft_info = field_types.get(field_key)
            expected_type = None
            if isinstance(ft_info, dict):
                expected_type = (
                    ft_info.get("field_type") or ft_info.get("type") or "Any"
                )

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

        if field_key in field_types:
            return {"exists": True}

        mutable = (
            explicit_types_dict.get(field_key, {}).get("mutable", True)
            if explicit_types_dict
            else True
        )
        unique = (
            explicit_types_dict.get(field_key, {}).get("unique", False)
            if explicit_types_dict
            else False
        )

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

    @staticmethod
    def detect_media_type(raw_v: str) -> Optional[str]:
        """
        Detect if a string contains base64-encoded media (image or audio).
        """
        content_to_check = raw_v

        if raw_v.startswith("data:") and "," in raw_v:
            content_to_check = raw_v.split(",", 1)[1]

        try:
            decoded = base64.b64decode(content_to_check, validate=True)
        except Exception:
            return None

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

        return None

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
            filename = url.split("/")[-1]
            base64_content = self.bucket_service.get_media(filename)
            return base64_content
        except Exception as e:
            raise ValueError(f"Failed to retrieve image from bucket: {str(e)}")

    def get_audio_from_bucket(self, url: str) -> Optional[str]:
        """Retrieve audio from bucket and return as base64."""
        try:
            filename = url.split("/")[-1]
            base64_content = self.bucket_service.get_media(filename)
            return base64_content
        except Exception as e:
            raise ValueError(f"Failed to retrieve audio from bucket: {str(e)}")

    @staticmethod
    def infer_type(raw_k, raw_v, explicit_type=None):
        """
        Infer the type of a field value.
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
            if is_pydantic_schema(explicit_type):
                try:
                    is_valid, error_msg = validate_value_against_pydantic_schema(
                        raw_v,
                        explicit_type,
                    )
                    if not is_valid:
                        raise ValueError(
                            f"Value does not match Pydantic schema for field '{raw_k}': {error_msg}",
                        )

                    schema = normalize_pydantic_schema(explicit_type)
                    return pydantic_schema_to_string(schema)
                except ValueError as e:
                    raise e
                except Exception as e:
                    raise ValueError(
                        f"Error processing Pydantic schema for field '{raw_k}': {str(e)}",
                    )

            return normalize_type_string(explicit_type)

        return infer_type_from_value(
            raw_v,
            media_detector=getattr(LogEventDAO, "detect_media_type", None),
        )

    def get_ids_by_filter(
        self,
        project_id: int,
        filters: Dict[str, Any],
        context_ids: Optional[List[int]] = None,
    ) -> List[int]:
        """
        Get log_event_ids that match the given filters for a project.
        """
        if not filters:
            return []

        query = select(LogEvent.id)
        if project_id is not None:
            query = query.where(LogEvent.project_id == project_id)

        if context_ids:
            query = query.join(
                LogEventContext,
                LogEventContext.log_event_id == LogEvent.id,
            ).where(LogEventContext.context_id.in_(context_ids))

        for key, value in filters.items():
            query = query.where(
                LogEvent.data[key].astext == str(value)
                if isinstance(value, str)
                else LogEvent.data[key] == cast(literal(value), JSONB),
            )
        result = self.session.execute(query)
        return [row[0] for row in result]

    def rename_field_in_logs(
        self,
        project_id: int,
        old_field_name: str,
        new_field_name: str,
        context_id: Optional[int] = None,
    ) -> None:
        """
        Rename a field across all log events in LogEvent.data.
        """
        try:
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

            self.session.execute(
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

            self.session.commit()

        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to rename field: {str(e)}")

    def _bulk_delete_gcs_media(
        self,
        log_event_ids: List[int],
        project_id: int,
        field_names: Optional[List[str]] = None,
    ):
        """
        Finds all image/audio fields in LogEvent.data for the given log_event_ids
        and deletes the corresponding files from GCS.
        """
        return
        if not log_event_ids:
            return

        gcs_url_prefix = (
            f"https://storage.googleapis.com/{self.bucket_service.bucket_name}/"
        )

        media_field_query = self.session.query(FieldType.field_name).filter(
            FieldType.project_id == project_id,
            FieldType.field_type.in_(("image", "audio")),
        )

        if field_names:
            media_field_query = media_field_query.filter(
                FieldType.field_name.in_(field_names),
            )

        media_fields = [row[0] for row in media_field_query.all()]

        if not media_fields:
            return

        log_events = (
            self.session.query(LogEvent.id, LogEvent.data)
            .filter(
                LogEvent.id.in_(log_event_ids),
                LogEvent.project_id == project_id,
            )
            .all()
        )

        urls_to_delete = []
        for log_event_id, data in log_events:
            if not data:
                continue
            for field_name in media_fields:
                value = data.get(field_name)
                if isinstance(value, str):
                    clean_value = value.strip("\"'")
                    if clean_value.startswith(gcs_url_prefix):
                        urls_to_delete.append((log_event_id, field_name, clean_value))

        if not urls_to_delete:
            return

        for log_event_id, field_name, url in urls_to_delete:
            try:
                filename = url.split("/")[-1]
                logging.warning(
                    f"Deleting GCS file: {filename} for log_event_id: {log_event_id}, field: {field_name}",
                )
                self.bucket_service.delete_media(filename)
            except Exception as e:
                logging.error(
                    f"Failed to delete GCS file for log_event_id {log_event_id}, field {field_name}: {str(e)}",
                )

    def get_next_composite_ids(
        self,
        project_id: int,
        context_id: int,
        unique_keys: Dict[str, str],
        provided_values: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Generate composite key values for unique_keys and auto-counting fields.
        """
        if not provided_values:
            return []

        context = self.session.query(Context).filter_by(id=context_id).one()
        auto_counting = context.auto_counting or {}
        unique_key_columns = context.unique_key_names or list(unique_keys.keys())

        counting_columns = list(auto_counting.keys())
        all_columns = unique_key_columns[:]
        for col in counting_columns:
            if col not in all_columns:
                all_columns.append(col)

        reserved_counters: Dict[tuple, int] = {}
        reserved_values: Dict[tuple, set[int]] = {}
        reserved_next: Dict[tuple, int] = {}

        def _lock_counter(col_name: str, parent_values: Dict[str, Any]) -> None:
            lock_key = json.dumps(
                {
                    "project_id": project_id,
                    "context_id": context_id,
                    "key": col_name,
                    "parents": parent_values,
                },
                sort_keys=True,
            )
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": lock_key},
            )

        def _next_counter_value(
            col_name: str,
            parent_values: Dict[str, Any],
        ) -> int:
            counter_key = (col_name, tuple(sorted(parent_values.items())))
            if col_name in unique_key_columns:
                if counter_key not in reserved_counters:
                    _lock_counter(col_name, parent_values)
                    query = (
                        self.session.query(
                            func.max(
                                cast(
                                    func.nullif(
                                        LogEvent.data.op("->>")(col_name),
                                        "null",
                                    ),
                                    Integer,
                                ),
                            ),
                        )
                        .join(
                            LogEventContext,
                            LogEventContext.log_event_id == LogEvent.id,
                        )
                        .filter(LogEventContext.context_id == context_id)
                    )
                    for parent_key, parent_value in parent_values.items():
                        query = query.filter(
                            LogEvent.data.op("->>")(parent_key) == str(parent_value),
                        )
                    max_val = query.scalar()
                    reserved_counters[counter_key] = (
                        max_val if max_val is not None else -1
                    ) + 1
                else:
                    reserved_counters[counter_key] += 1
                return reserved_counters[counter_key]

            if counter_key not in reserved_values:
                _lock_counter(col_name, parent_values)
                values_query = (
                    self.session.query(
                        cast(
                            func.nullif(LogEvent.data.op("->>")(col_name), "null"),
                            Integer,
                        ),
                    )
                    .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
                    .filter(LogEventContext.context_id == context_id)
                )
                for parent_key, parent_value in parent_values.items():
                    values_query = values_query.filter(
                        LogEvent.data.op("->>")(parent_key) == str(parent_value),
                    )
                existing_values = {
                    row[0] for row in values_query.all() if row[0] is not None
                }
                reserved_values[counter_key] = existing_values
                reserved_next[counter_key] = 0

            next_value = reserved_next[counter_key]
            while next_value in reserved_values[counter_key]:
                next_value += 1
            reserved_values[counter_key].add(next_value)
            reserved_next[counter_key] = next_value + 1
            return next_value

        completed = []
        existing_parent_cache: Dict[tuple, bool] = {}

        def _parent_values_exist(
            parent_values: Dict[str, Any],
            completed_rows: List[Dict[str, Any]],
        ) -> bool:
            if not parent_values:
                return True

            cache_key = tuple(sorted(parent_values.items()))
            cached = existing_parent_cache.get(cache_key)
            if cached is not None:
                return cached

            for row in completed_rows:
                if all(row.get(k) == v for k, v in parent_values.items()):
                    existing_parent_cache[cache_key] = True
                    return True

            existing = (
                self.session.query(LogEvent.id)
                .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
                .filter(LogEventContext.context_id == context_id)
                .filter(
                    LogEvent.data.op("@>")(
                        cast(literal(json.dumps(parent_values)), JSONB),
                    ),
                )
                .first()
            )
            exists = existing is not None
            existing_parent_cache[cache_key] = exists
            return exists

        for provided_value in provided_values:
            row_values = dict(provided_value or {})
            provided_keys = set(row_values.keys())

            for key in unique_keys.keys():
                if key not in auto_counting and key not in row_values:
                    raise ValueError(
                        f"Missing value for composite key column '{key}'",
                    )

            remaining = [c for c in counting_columns if c not in row_values]
            while remaining:
                progressed = False
                for col_name in list(remaining):
                    parent_col = auto_counting.get(col_name)
                    parent_values = {}
                    current_col = parent_col
                    missing_parent = False
                    while current_col is not None:
                        if current_col in row_values:
                            parent_values[current_col] = row_values[current_col]
                            current_col = auto_counting.get(current_col)
                            continue
                        missing_parent = True
                        break

                    if missing_parent:
                        continue

                    if parent_values and all(
                        key in provided_keys for key in parent_values
                    ):
                        if not _parent_values_exist(parent_values, completed):
                            raise ValueError(
                                "Cannot generate auto-counting value for "
                                f"'{col_name}' because parent values "
                                f"{parent_values} does not exist.",
                            )

                    row_values[col_name] = _next_counter_value(
                        col_name,
                        parent_values,
                    )
                    remaining.remove(col_name)
                    progressed = True

                if not progressed:
                    raise ValueError(
                        "Missing parent values for auto-counting columns.",
                    )

            ordered_row = {}
            for col_name in all_columns:
                if col_name in row_values:
                    ordered_row[col_name] = row_values[col_name]
            completed.append(ordered_row)

        if unique_key_columns:
            seen = set()
            for row in completed:
                combo = tuple(row.get(col) for col in unique_key_columns)
                if None in combo:
                    raise ValueError(
                        "Composite key columns must all have values.",
                    )
                if combo in seen:
                    raise ValueError(
                        f"Duplicate composite key values in batch: {combo}",
                    )
                seen.add(combo)

            or_conditions = []
            for row in completed:
                combo = {col: row[col] for col in unique_key_columns}
                or_conditions.append(
                    LogEvent.data.op("@>")(cast(literal(json.dumps(combo)), JSONB)),
                )

            if or_conditions:
                existing = (
                    self.session.query(LogEvent.id)
                    .join(
                        LogEventContext,
                        LogEventContext.log_event_id == LogEvent.id,
                    )
                    .filter(LogEventContext.context_id == context_id)
                    .filter(or_(*or_conditions))
                    .first()
                )
                if existing:
                    raise ValueError(
                        "Duplicate composite key already exists for this context.",
                    )

        return completed

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
        """
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
            if isinstance(value, str):
                current_enum_values = field_type.enum_values
                if value not in current_enum_values:
                    if field_type.enum_restrict:
                        raise ValueError(
                            f"Value '{value}' is not in allowed enum values for field '{key}': {current_enum_values}",
                        )
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
                            enum_values=FieldType.enum_values.concat(new_values),
                        )
                    )
                    self.session.execute(stmt)

            if enum_restrict:
                field_type.enum_restrict = enum_restrict

        else:
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
        """
        if not segments:
            return new_value

        current = doc
        parent = None
        final_key = segments[-1]

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

        if isinstance(current, dict):
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

    def bulk_update(
        self,
        updates: List[Dict[str, Any]],
        overwrite: bool = False,
        field_types: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Update multiple LogEvent.data JSONB fields with partial success support.
        """
        import json

        if not updates:
            return {"successful_update_ids": [], "failed": []}

        field_types = field_types or {}
        updates_by_log_id: Dict[int, List[Dict[str, Any]]] = {}
        for update_item in updates:
            le_id = update_item.get("log_event_id")
            if le_id:
                updates_by_log_id.setdefault(le_id, []).append(update_item)

        update_result = {"successful_update_ids": [], "failed": []}

        if not updates_by_log_id:
            return update_result

        all_log_ids = list(updates_by_log_id.keys())
        log_events = (
            self.session.query(LogEvent)
            .filter(LogEvent.id.in_(all_log_ids))
            .with_for_update()
            .all()
        )

        log_event_map = {le.id: le for le in log_events}

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

        enum_field_info: Dict[tuple, Dict[str, Any]] = {}
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

        field_type_map: Dict[tuple, FieldType] = {}
        if enum_field_info:
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

        fields_to_expand: Dict[tuple, List[str]] = {}
        fields_to_create: List[Dict[str, Any]] = []
        restricted_enum_errors: Dict[tuple, str] = {}

        for field_key, info in enum_field_info.items():
            project_id, context_id, field_name = field_key
            values_in_batch = info["values"]
            enum_values = info.get("enum_values") or []
            enum_restrict = info.get("enum_restrict", False)

            ft = field_type_map.get((project_id, context_id, field_name))
            if ft:
                existing_values = ft.enum_values or []
                new_values = [v for v in values_in_batch if v not in existing_values]

                if new_values and ft.enum_restrict:
                    restricted_enum_errors[
                        field_key
                    ] = f"Value '{new_values[0]}' is not in allowed enum values for field '{field_name}': {existing_values}"
                elif new_values:
                    fields_to_expand[field_key] = new_values
            else:
                for v in values_in_batch:
                    if enum_values and v not in enum_values and enum_restrict:
                        restricted_enum_errors[
                            field_key
                        ] = f"Value '{v}' is not in allowed enum values for field '{field_name}': {enum_values}"
                        break
                else:
                    fields_to_create.append(
                        {
                            "project_id": project_id,
                            "context_id": context_id,
                            "field_name": field_name,
                            "enum_values": list(
                                set((enum_values or []) + list(values_in_batch)),
                            ),
                            "enum_restrict": enum_restrict,
                        },
                    )

        if restricted_enum_errors:
            for field_key, err in restricted_enum_errors.items():
                update_result["failed"].append(
                    {
                        "log_event_id": None,
                        "error": err,
                    },
                )
            return update_result

        if fields_to_expand:
            for field_key, new_values in fields_to_expand.items():
                proj_id, ctx_id, field_name = field_key
                ft = field_type_map.get(field_key)
                if ft:
                    updated_values = list(set(ft.enum_values + new_values))
                    stmt = (
                        update(FieldType)
                        .where(
                            FieldType.project_id == proj_id,
                            FieldType.field_name == field_name,
                            FieldType.context_id == ctx_id,
                        )
                        .values(enum_values=updated_values)
                    )
                    self.session.execute(stmt)

        if fields_to_create:
            for field_info in fields_to_create:
                self.session.add(
                    FieldType(
                        project_id=field_info["project_id"],
                        context_id=field_info["context_id"],
                        field_name=field_info["field_name"],
                        field_type="enum",
                        field_category="entry",
                        enum_values=field_info["enum_values"],
                        enum_restrict=field_info["enum_restrict"],
                    ),
                )

        batch_updates: List[tuple] = []

        for log_event_id, log_updates in updates_by_log_id.items():
            if log_event_id not in log_event_map:
                continue
            log_event = log_event_map[log_event_id]

            try:
                current_data = dict(log_event.data or {})
                update_data = {}

                for update_data_item in log_updates:
                    key = update_data_item.get("key")
                    value = update_data_item.get("value")
                    explicit_types = update_data_item.get("explicit_types", {})
                    overwrite_update = update_data_item.get("overwrite", overwrite)

                    if not key:
                        continue

                    ft_info = field_types.get(key)
                    if ft_info and not ft_info.get("mutable", True):
                        raise ImmutableFieldError(f"Field '{key}' is immutable")

                    key_explicit_type = explicit_types.get(key, {})
                    inferred_type = key_explicit_type.get("type")

                    if inferred_type is None:
                        inferred_type = self.infer_type(key, value)

                    if inferred_type == "enum":
                        self._handle_enum_field_type(
                            project_id=update_data_item.get("project_id")
                            or log_event.project_id,
                            context_id=update_data_item.get("context_id"),
                            key=key,
                            value=value,
                            enum_values=key_explicit_type.get("values"),
                            enum_restrict=key_explicit_type.get("restrict", False),
                        )
                        inferred_type = "str"

                    if inferred_type == "image" and isinstance(value, str):
                        value = self.upload_image_to_bucket(value)
                    elif inferred_type == "audio" and isinstance(value, str):
                        value = self.upload_audio_to_bucket(value)

                    if key in current_data and not overwrite_update:
                        if current_data[key] != value:
                            raise OverwriteError(
                                f"Field '{key}' already exists and overwrite is False",
                            )

                    update_data[key] = value

                if not update_data:
                    continue

                update_json = json.dumps(update_data)
                batch_updates.append((log_event_id, update_json))
                update_result["successful_update_ids"].append(log_event_id)

            except OverwriteError as e:
                update_result["failed"].append(
                    {
                        "log_event_id": log_event_id,
                        "error": (
                            "Existing value cannot be overwritten because overwrite is set to False: "
                            f"{str(e)}"
                        ),
                    },
                )
            except ImmutableFieldError as e:
                update_result["failed"].append(
                    {
                        "log_event_id": log_event_id,
                        "error": (
                            "Field is immutable and cannot be modified: " f"{str(e)}"
                        ),
                    },
                )
            except Exception as e:
                update_result["failed"].append(
                    {
                        "log_event_id": log_event_id,
                        "error": f"Failed to update log: {str(e)}",
                    },
                )

        if batch_updates:
            ids_array = [log_id for log_id, _ in batch_updates]
            data_array = [json_str for _, json_str in batch_updates]

            update_sql = text(
                """
                UPDATE log_event
                SET data = COALESCE(log_event.data, '{}'::jsonb) || update_data.data::jsonb,
                    updated_at = :now
                FROM (
                    SELECT unnest(:ids) AS id,
                           unnest(:data) AS data
                ) AS update_data
                WHERE log_event.id = update_data.id
                """,
            )
            self.session.execute(
                update_sql,
                {
                    "now": now,
                    "ids": ids_array,
                    "data": data_array,
                },
            )

        self.session.commit()
        return update_result

    def apply_jsonb_patch(
        self,
        patches: List[Dict[str, Any]],
        overwrite: bool = False,
        field_types: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Apply JSONB patches to nested paths within LogEvent.data.
        """
        result = {
            "successful_update_ids": [],
            "failed": [],
        }

        if not patches:
            return result

        field_types = field_types or {}

        try:
            now = datetime.now(timezone.utc)

            grouped = {}
            for patch in patches:
                le_id = patch.get("log_event_id")
                base_key = patch.get("base_key")
                if not le_id or not base_key:
                    continue
                grouped.setdefault((le_id, base_key), []).append(patch)

            all_log_ids = list(set(le_id for (le_id, _) in grouped.keys()))
            log_events = (
                self.session.query(LogEvent)
                .filter(LogEvent.id.in_(all_log_ids))
                .with_for_update()
                .all()
            )

            log_event_map = {le.id: le for le in log_events}

            for le_id in all_log_ids:
                if le_id not in log_event_map:
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": f"LogEvent not found for log_event_id={le_id}",
                        },
                    )

            batch_patch_updates: List[tuple] = []
            processed_log_ids: set = set()
            failed_log_ids: set = set()

            for (le_id, base_key), group in grouped.items():
                if le_id not in log_event_map:
                    failed_log_ids.add(le_id)
                    continue

                try:
                    log_event = log_event_map[le_id]

                    ft_info = field_types.get(base_key)
                    if ft_info and not ft_info.get("mutable", True):
                        raise ImmutableFieldError(f"Field '{base_key}' is immutable")

                    current_data = dict(log_event.data or {})

                    if base_key not in current_data:
                        current_data[base_key] = {}

                    current_doc = copy.deepcopy(current_data.get(base_key, {}))

                    for patch in group:
                        path_str = patch.get("path_segments", "")
                        new_value = patch.get("new_value")
                        patch_overwrite = patch.get("overwrite", overwrite)

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

                        current_doc = self._apply_patch_to_doc(
                            current_doc,
                            segments,
                            new_value,
                            patch_overwrite,
                        )

                    update_json = json.dumps({base_key: current_doc})
                    batch_patch_updates.append((le_id, update_json))
                    processed_log_ids.add(le_id)

                except OverwriteError as e:
                    failed_log_ids.add(le_id)
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": (
                                "Existing value cannot be overwritten because overwrite is set to False: "
                                f"{str(e)}"
                            ),
                        },
                    )
                except ImmutableFieldError as e:
                    failed_log_ids.add(le_id)
                    result["failed"].append(
                        {
                            "log_event_id": le_id,
                            "error": (
                                "Field is immutable and cannot be modified: "
                                f"{str(e)}"
                            ),
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

            if batch_patch_updates:
                from orchestra.web.api.log.utils.logging_utils import extract_key_order

                ids_array = [log_id for log_id, _ in batch_patch_updates]
                data_array = [json_str for _, json_str in batch_patch_updates]

                key_order_array = []
                for _, json_str in batch_patch_updates:
                    update_data = json.loads(json_str)
                    new_key_order = extract_key_order(update_data)
                    key_order_array.append(json.dumps(new_key_order))

                update_sql = text(
                    """
                    UPDATE log_event
                    SET data = COALESCE(log_event.data, '{}'::jsonb) || update_data.data::jsonb,
                        key_order = COALESCE(log_event.key_order, '{}'::jsonb) || update_data.key_order::jsonb,
                        updated_at = :now
                    FROM (
                        SELECT unnest(:ids) AS id,
                               unnest(:data) AS data,
                               unnest(:key_orders) AS key_order
                    ) AS update_data
                    WHERE log_event.id = update_data.id
                    """,
                )

                self.session.execute(
                    update_sql,
                    {
                        "now": now,
                        "ids": ids_array,
                        "data": data_array,
                        "key_orders": key_order_array,
                    },
                )

            self.session.commit()

            for log_id in processed_log_ids:
                if log_id not in failed_log_ids:
                    result["successful_update_ids"].append(log_id)

        except Exception as e:
            self.session.rollback()
            result["failed"].append(
                {
                    "log_event_id": None,
                    "error": str(e),
                },
            )

        return result

    def recompute_derived_logs(
        self,
        template,
        log_ids: List[int],
        json_encoder: json.JSONEncoder,
        field_type_dao=None,
    ) -> int:
        """
        Recompute derived log values by materializing them directly into LogEvent.data.
        """
        if not log_ids:
            return 0

        try:
            from orchestra.web.api.log.python2SQL import (
                _compute_expression,
                _extract_placeholders,
                _substitute_placeholders,
                str_filter_exp_to_dict,
            )

            placeholders = _extract_placeholders(template.equation)
            resolved_ids = {}
            for p in placeholders:
                log_key = p.split(":")[0]
                resolved_ids[log_key] = log_ids

            filter_expr, alias_to_key_map = _substitute_placeholders(
                template.equation,
                resolved_ids,
            )
            filter_dict = str_filter_exp_to_dict(filter_expr)

            computed_values = _compute_expression(
                filter_dict,
                LogEvent,
                self.session,
                log_ids,
            )

            if not computed_values:
                return 0

            non_null_val = None
            updates_count = 0

            for log_event_id, value in computed_values:
                try:
                    val = json.loads(json.dumps(value, cls=json_encoder))
                    if val is not None:
                        non_null_val = val

                    stmt = (
                        update(LogEvent)
                        .where(LogEvent.id == log_event_id)
                        .values(
                            data=LogEvent.data.concat(
                                func.jsonb_build_object(template.key, val),
                            ),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                    self.session.execute(stmt)
                    updates_count += 1
                except Exception as e:
                    logger.warning(
                        f"Failed to recompute derived log for log_event_id={log_event_id}: {e}",
                    )
                    continue

            self.session.commit()

            if field_type_dao and non_null_val is not None:
                try:
                    field_type_dao.create_field_type_if_absent(
                        project_id=template.project_id,
                        field_name=template.key,
                        value=non_null_val,
                        context_id=template.context_id,
                        field_category="derived_entry",
                        infer_type=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to create field type for '{template.key}': {e}",
                    )

            return updates_count

        except Exception as e:
            self.session.rollback()
            logger.error(f"Error in recompute_derived_logs: {e}")
            raise e

    def bulk_merge_data(self, entries: List[Dict[str, Any]]) -> None:
        """
        Bulk merge entry key/value pairs into LogEvent.data.
        """
        if not entries:
            return

        entries_by_log = defaultdict(dict)
        for entry in entries:
            log_event_id = entry.get("log_event_id")
            key = entry.get("key")
            if not log_event_id or key is None:
                continue
            entries_by_log[log_event_id][key] = entry.get("value")

        if not entries_by_log:
            return

        update_values = [
            (le_id, json.dumps(fields)) for le_id, fields in entries_by_log.items()
        ]

        self.session.execute(
            text(
                """
                UPDATE log_event le
                SET data = COALESCE(le.data, '{}'::jsonb) || v.fields_json::jsonb,
                    updated_at = :now
                FROM (SELECT unnest(:ids) AS id, unnest(:fields) AS fields_json) AS v
                WHERE le.id = v.id
                """,
            ),
            {
                "now": datetime.now(timezone.utc),
                "ids": [v[0] for v in update_values],
                "fields": [v[1] for v in update_values],
            },
        )
        self.session.flush()

    def filter(
        self,
        id: Optional[int] = None,
        project_id: Optional[int] = None,
        context_id: Optional[int] = None,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> List[LogEvent]:
        query = select(LogEvent).distinct()

        if id:
            query = query.where(LogEvent.id == id)
        if project_id:
            query = query.where(LogEvent.project_id == project_id)
        if context_id:
            query = query.join(LogEventContext).where(
                LogEventContext.context_id == context_id,
            )

        query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        query = query.order_by(LogEvent.created_at)

        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        project_id: Optional[int] = None,
    ) -> None:
        query = select(LogEvent)
        query = query.where(LogEvent.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if project_id:
                setattr(entry, "project_id", project_id)

    def delete(self, id: Union[int, List[int]]):
        ids = id if isinstance(id, list) else [id]
        if not ids:
            return

        try:
            # Delete associated GCS media BEFORE deleting DB records
            # Get project_id from first log event for the new function signature
            first_log_event = (
                self.session.query(LogEvent).filter(LogEvent.id.in_(ids)).first()
            )
            if first_log_event:
                self._bulk_delete_gcs_media(ids, first_log_event.project_id)

            # First, delete the association rows referencing these log events
            self.session.query(LogEventContext).filter(
                LogEventContext.log_event_id.in_(ids),
            ).delete(synchronize_session=False)

            # Then, delete the log event(s) themselves (which cascades to Log and JSONLog in the DB)
            self.session.query(LogEvent).filter(
                LogEvent.id.in_(ids),
            ).delete(synchronize_session=False)

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete log events: {e}")

    def get_ts(self, id: int) -> Optional[datetime]:
        query = (
            select(Project.created_at)
            .join(LogEvent, Project.id == LogEvent.project_id)
            .where(LogEvent.id == id)
        )
        rows = self.session.execute(query).fetchone()
        return rows[0] if rows is not None else None

    def get_user_id(self, id: int) -> Optional[str]:
        query = (
            select(Project.user_id)
            .join(LogEvent, Project.id == LogEvent.project_id)
            .where(LogEvent.id == id)
        )
        rows = self.session.execute(query).fetchone()
        return rows[0] if rows is not None else None

    def get_user_and_project_id(self, id: int) -> Optional[str]:
        query = (
            select(Project.user_id, Project.id)
            .join(LogEvent, Project.id == LogEvent.project_id)
            .where(LogEvent.id == id)
        )
        rows = self.session.execute(query).fetchone()
        return rows if rows is not None else (None, None)

    def get_user_and_project_ids_batch(
        self,
        ids: List[int],
    ) -> Dict[int, tuple]:
        """
        Batch fetch user_id and project_id for multiple log_event IDs.

        Args:
            ids: List of log_event IDs to fetch

        Returns:
            Dictionary mapping log_event_id -> (user_id, project_id)
            Missing IDs are excluded from the result (caller checks for missing keys)
        """
        if not ids:
            return {}

        query = (
            select(LogEvent.id, Project.user_id, Project.id)
            .join(Project, Project.id == LogEvent.project_id)
            .where(LogEvent.id.in_(ids))
        )
        rows = self.session.execute(query).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}
