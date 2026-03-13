import json
import random
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, HTTPException, Request
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Integer,
    String,
    Text,
    asc,
    case,
    cast,
    desc,
    func,
    lateral,
    literal,
    or_,
    select,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import ColumnClause
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Context,
    Embedding,
    FieldType,
    LogEvent,
    LogEventContext,
)
from orchestra.settings import settings
from orchestra.web.api.log.python2SQL.operators import _create_truthiness_condition
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.utils.http_responses import not_found

from ..python2SQL import STR_TO_SQL_TYPES
from ..python2SQL.core import build_sql_query
from ..python2SQL.helpers import _select_value
from ..python2SQL.parsers import str_filter_exp_to_dict

__all__ = [
    "_get_logs_query",
    "_get_logs_query",
    "create_logs_internal",
    "_build_unified_logs_subquery",
    "_flatten_fields",
    "_format_flat_logs",
    "_format_logs",
    "_get_final_logs",
    "is_image_field",
    "is_audio_field",
    "_join_logs",
    "extract_key_order",
    "reorder_nested_dict",
]


def extract_key_order(data: Any, path: str = "_root") -> Dict[str, List[str]]:
    """
    Recursively extract key ordering from nested dictionaries.

    This function traverses a nested dictionary structure and records the key order
    at each level. This is needed because PostgreSQL JSONB sorts keys alphabetically,
    so we need to store the original insertion order separately.

    Args:
        data: The data to extract key ordering from (dict, list, or primitive)
        path: The current path in the nested structure (e.g., "_root", "level1", "level1.nested")

    Returns:
        Dict mapping paths to lists of keys in their original order.
        Example: {"_root": ["c", "b", "a"], "c": ["inner1", "inner2"], "c.inner1": ["deep"]}
    """
    result = {}

    if isinstance(data, dict) and data:
        # Record the key order at this level
        result[path] = list(data.keys())

        # Recursively process nested dicts
        for key, value in data.items():
            child_path = key if path == "_root" else f"{path}.{key}"
            child_order = extract_key_order(value, child_path)
            result.update(child_order)

    elif isinstance(data, list):
        # Process list items (only dicts within lists)
        for i, item in enumerate(data):
            if isinstance(item, dict):
                child_path = f"{path}[{i}]"
                child_order = extract_key_order(item, child_path)
                result.update(child_order)

    return result


def reorder_nested_dict(
    data: Any,
    key_order: Optional[Dict[str, List[str]]],
    path: str = "_root",
) -> Any:
    """
    Recursively reorder dictionary keys based on stored key_order.

    This function reconstructs the original key ordering of nested dictionaries
    using the key_order metadata that was stored during log creation.

    Args:
        data: The data to reorder (dict, list, or primitive)
        key_order: Dict mapping paths to lists of keys in their original order.
                   If None, returns data unchanged.
        path: The current path in the nested structure

    Returns:
        The data with dict keys reordered according to key_order.
        Keys not in key_order are appended at the end.
    """
    if key_order is None:
        return data

    if isinstance(data, dict):
        order = key_order.get(path, [])
        result = {}

        # Add keys in original order
        for k in order:
            if k in data:
                child_path = k if path == "_root" else f"{path}.{k}"
                result[k] = reorder_nested_dict(data[k], key_order, child_path)

        # Add any new keys not in order (appended at end)
        for k in data:
            if k not in result:
                child_path = k if path == "_root" else f"{path}.{k}"
                result[k] = reorder_nested_dict(data[k], key_order, child_path)

        return result

    elif isinstance(data, list):
        # Process list items
        result = []
        for i, item in enumerate(data):
            child_path = f"{path}[{i}]"
            result.append(reorder_nested_dict(item, key_order, child_path))
        return result

    else:
        # Primitive value - return as-is
        return data


def enforce_types(
    field_name,
    value,
    *,
    field_types,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    project_id: int,
    batch_index=None,
    explicit_types=None,
    context_id=None,
    is_param: bool = False,
):
    """
    Module-level type enforcement function (extracted from create_logs_internal).

    - Fields with type "Any": Accept any value (mixed types)
    - Fields with strict type: Require explicit_type (if provided) to match field type
    - New fields: Created with DEFAULT_FIELD_TYPE ("Any") unless explicit type provided
    - "NoneType": Treated as a weak type – None is allowed for any field type
    """
    from orchestra.web.api.log.utils.type_utils import is_untyped_field

    # Extract explicit_type if provided in explicit_types (can be str or JSON schema)
    explicit_type_spec = None
    enum_values = None
    enum_restrict = False

    if explicit_types and field_name in explicit_types:
        field_spec = explicit_types[field_name]
        if isinstance(field_spec, dict):
            explicit_type_spec = field_spec.get("type")
            enum_values = field_spec.get("values")
            enum_restrict = field_spec.get("restrict", False)
        elif isinstance(field_spec, str):
            explicit_type_spec = field_spec

    # Get field info if it exists
    field_info = field_types.get(field_name)

    if field_info:
        # Field exists - check category first
        existing_category = field_info["field_category"]
        new_category = "param" if is_param else "entry"
        if existing_category != new_category:
            new_article = "an" if new_category == "entry" else "a"
            existing_article = "an" if existing_category == "entry" else "a"
            raise HTTPException(
                status_code=400,
                detail=f"Field '{field_name}' already exists as {existing_article} {existing_category}. Cannot create it as {new_article} {new_category}.",
            )

        # Check field type
        field_type = field_info["field_type"]

        # Case 1: Field is untyped (DEFAULT_FIELD_TYPE/"Any") - accept any value (mixed types)
        if is_untyped_field(field_type):
            # Field is untyped/accepts mixed types (including None)
            # We don't update the field_type (it stays "Any")
            return

        # Case 2: Field has strict type (not "Any") - check value type
        from orchestra.web.api.log.utils.type_utils import (
            is_pydantic_schema,
            normalize_pydantic_schema,
            types_match,
            validate_value_against_pydantic_schema,
        )

        # Always try to validate against FIELD TYPE if it's a valid Pydantic schema
        tried_field_schema = False
        if is_pydantic_schema(field_type):
            tried_field_schema = True
            try:
                field_schema = normalize_pydantic_schema(field_type)
                ok_field, err_field = validate_value_against_pydantic_schema(
                    value,
                    field_schema,
                )
            except Exception as e:
                ok_field, err_field = (False, str(e))
            if not ok_field:
                batch_info = (
                    f" (in batch entry {batch_index})"
                    if batch_index is not None
                    else ""
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Type validation against field schema failed for '{field_name}'{batch_info}: {err_field}",
                )

        # If explicit_type provided, also validate against EXPLICIT schema when it's Pydantic
        if explicit_type_spec is not None and is_pydantic_schema(explicit_type_spec):
            schema = normalize_pydantic_schema(explicit_type_spec)
            ok_explicit, err_explicit = validate_value_against_pydantic_schema(
                value,
                schema,
            )
            if not ok_explicit:
                batch_info = (
                    f" (in batch entry {batch_index})"
                    if batch_index is not None
                    else ""
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Type validation against explicit schema failed for '{field_name}'{batch_info}: {err_explicit}",
                )

        # If neither field nor explicit were Pydantic schemas, fall back to string typing logic
        if not tried_field_schema and not (
            explicit_type_spec is not None and is_pydantic_schema(explicit_type_spec)
        ):
            if explicit_type_spec is not None:
                comparable_type = str(explicit_type_spec)
                if not types_match(field_type, comparable_type):
                    batch_info = (
                        f" (in batch entry {batch_index})"
                        if batch_index is not None
                        else ""
                    )
                    raise HTTPException(
                        status_code=400,
                        detail=f"Type mismatch for field '{field_name}'{batch_info}: field has strict type '{field_type}', but explicit_type '{comparable_type}' was provided.",
                    )
            else:
                # When the value's Python runtime type is compatible with
                # the declared field type, skip content-based inference.
                # This prevents e.g. a date-formatted string "2024-01-15"
                # from being re-classified as 'date' and rejected when the
                # field is declared as 'str'.
                #
                # Compatibility follows standard Python numeric semantics:
                #   - bool  → only 'bool' (not promoted to int/float)
                #   - int   → 'int' or 'float' (numeric widening)
                #   - float → 'float'
                #   - str   → 'str'
                #
                # Container types (list, dict) are excluded because they
                # need inference to validate inner/element types
                # (e.g. List[int] vs List[str]).
                _PYTHON_TYPE_TO_COMPATIBLE_FIELDS: dict[type, tuple[str, ...]] = {
                    bool: ("bool",),
                    int: ("int", "float"),
                    float: ("float",),
                    str: ("str",),
                }
                compatible_fields = _PYTHON_TYPE_TO_COMPATIBLE_FIELDS.get(
                    type(value),
                )
                if compatible_fields is not None and any(
                    types_match(field_type, ft) for ft in compatible_fields
                ):
                    pass  # runtime type compatible with declared type
                else:
                    inferred_type = LogEventDAO.infer_type(
                        field_name,
                        value,
                        explicit_type=None,
                    )
                    if not types_match(field_type, inferred_type):
                        batch_info = (
                            f" (in batch entry {batch_index})"
                            if batch_index is not None
                            else ""
                        )
                        raise HTTPException(
                            status_code=400,
                            detail=f"Type mismatch for field '{field_name}'{batch_info}: field has strict type '{field_type}', but value has inferred type '{inferred_type}'. Value: {str(value)[:100]}",
                        )
    else:
        # Field doesn't exist - create it
        # New policy: We CAN create new fields, but we CANNOT modify existing fields
        field_spec = explicit_types.get(field_name, {}) if explicit_types else {}
        mutable = (
            field_spec.get("mutable", True) if isinstance(field_spec, dict) else True
        )
        unique = (
            field_spec.get("unique", False) if isinstance(field_spec, dict) else False
        )

        # Extract explicit type if provided
        explicit_field_type = None
        if isinstance(field_spec, dict):
            explicit_field_type = field_spec.get("type")  # str or JSON schema
        elif isinstance(field_spec, str):
            explicit_field_type = field_spec

        # If in a versioned context, force mutable=True
        if context_id and context_dao.is_versioned(context_id):
            mutable = True

        # Validate value against Pydantic schema BEFORE creating the field
        # This ensures invalid data is rejected even for new fields
        if explicit_field_type is not None:
            from orchestra.web.api.log.utils.type_utils import (
                is_pydantic_schema,
                normalize_pydantic_schema,
                validate_value_against_pydantic_schema,
            )

            if is_pydantic_schema(explicit_field_type):
                try:
                    schema = normalize_pydantic_schema(explicit_field_type)
                    ok, err = validate_value_against_pydantic_schema(value, schema)
                except Exception as e:
                    ok, err = (False, str(e))
                if not ok:
                    batch_info = (
                        f" (in batch entry {batch_index})"
                        if batch_index is not None
                        else ""
                    )
                    raise HTTPException(
                        status_code=400,
                        detail=f"Type validation against explicit schema failed for '{field_name}'{batch_info}: {err}",
                    )

        # Create field - type precedence:
        # 1. Explicit type (from explicit_types) → strict typing
        # 2. No explicit type → "Any" (untyped)
        # Note: infer_type=False because we're in create_logs_internal with explicit_types support
        field_type_dao.create_field_type_if_absent(
            project_id,
            field_name,
            value,
            mutable=mutable,
            unique=unique,
            field_category="param" if is_param else "entry",
            context_id=context_id,
            field_type=explicit_field_type,  # Pass explicit type if provided
            enum_values=enum_values,
            enum_restrict=enum_restrict,
            infer_type=False,  # Don't infer - default to "Any" if no explicit type
        )


def _paginate_events(
    session,
    base_event_q,
    order_by_cols,
    limit,
    offset,
    randomize=False,
    seed="42",
    has_joins=False,
):
    """
    Fast, index-friendly pagination helper that:
    1. Materializes all filtered LogEvent IDs into a sub-query
    2. Gets the total row count before slicing
    3. Returns a second sub-query with row_number for order preservation
    """
    # If we have joins (for sorting), we need to handle differently
    if has_joins and order_by_cols:
        # Build paginated query with joins preserved (use optimized versions)
        pag_query = base_event_q.add_columns(
            func.row_number().over(order_by=order_by_cols).label("row_num"),
        ).order_by(*order_by_cols)

        if limit:
            pag_query = pag_query.limit(limit)
        if offset:
            pag_query = pag_query.offset(offset)

        return pag_query.subquery("paginated_ids_subq")

    # Original logic for simple queries
    relevant_sq = base_event_q.subquery("relevant_log_events")

    # Build the ordered/limited ID list
    if randomize:
        random_key = func.md5(cast(relevant_sq.c.id, String) + literal(seed))
        order_by_cols = [random_key]
    if not order_by_cols:
        order_by_cols = [desc(relevant_sq.c.id)]

    paginated_sq = select(
        relevant_sq.c.id.label("id"),
        func.row_number().over(order_by=order_by_cols).label("row_num"),
    ).order_by(*order_by_cols)

    if limit:
        paginated_sq = paginated_sq.limit(limit)
    if offset:
        paginated_sq = paginated_sq.offset(offset)

    return paginated_sq.subquery("paginated_ids_subq")


#########################
# Logs Utils            #
#########################


def _build_unified_logs_limited(
    session,
    ids_subq: Subquery,
    context_id: Optional[int] = None,
) -> Subquery:
    """
    Build unified logs subquery limited to the specified log_event_ids.
    """
    # Pass ID list through event_ids parameter to enable index-based filtering.
    id_only_sq = select(ids_subq.c.id).subquery("page_ids")
    return _build_unified_logs_subquery(
        session=session,
        event_ids=id_only_sq,
        context_id=context_id,
    )


def _build_sort_criteria(
    val_col: ColumnClause,
    sort_key: str,
    field_types: Dict[str, str],
):
    from .type_utils import get_sql_casting_type

    # If recognized type => cast
    if sort_key in field_types:
        raw_pytype = field_types[sort_key]
        # Normalize type to SQL-compatible (handles Pydantic schemas, Optional[T], etc.)
        pytype = get_sql_casting_type(raw_pytype) or raw_pytype
        cast_type = STR_TO_SQL_TYPES.get(pytype, None)
        if cast_type is not None:
            if pytype in ("datetime", "date", "time"):
                sort_expr = case(
                    (val_col.is_(None), None),
                    (val_col == text("'null'::jsonb"), None),
                    else_=cast(cast(val_col, String), cast_type),
                )
            elif pytype in ("dict", "list"):
                # For JSONB types, no need for additional casting
                sort_expr = val_col
            else:
                # For other data types (bool, int, float, str)
                sort_expr = case(
                    (val_col.is_(None), None),
                    (val_col == text("'null'::jsonb"), None),
                    else_=cast(val_col, cast_type),
                )
        else:
            sort_expr = val_col
    else:
        sort_expr = val_col

    return sort_expr


def _build_sort_clauses(
    session,
    log_event_query,
    field_types,
    sorting,
    relevant_log_events,
    sort_val_sqs,
    sort_criteria,
):
    """
    Helper function to build sorting clauses for log queries.
    Extracts the sorting logic from _get_logs_query for reusability.
    """
    is_vector_sort = False
    vector_sort_details = {}

    if sorting:
        sort_dict = json.loads(sorting)

        # Applies when sorting by a single vector similarity metric.
        if isinstance(sort_dict, dict) and len(sort_dict) == 1:
            sort_key, mode = next(iter(sort_dict.items()))
            if mode not in ("ascending", "descending"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Sort mode must be 'ascending' or 'descending', got {mode}.",
                )
            try:
                expr_dict = str_filter_exp_to_dict(
                    sort_key,
                    field_names=list(field_types.keys()),
                )
            except Exception:
                # not parseable => fall back
                expr_dict = None

            if (
                isinstance(expr_dict, dict)
                and expr_dict.get("operand") in ("cosine", "l2", "ip")
                and isinstance(expr_dict.get("lhs"), dict)
                and expr_dict["lhs"].get("type") == "identifier"
                and isinstance(expr_dict.get("rhs"), dict)
                and expr_dict["rhs"].get("operand")
                in ("embed", "embed_image")  # Support both text and image embeddings
            ):
                is_vector_sort = True
                vector_sort_details = {
                    "expr_dict": expr_dict,
                    "operand": expr_dict["operand"],
                    "mode": mode,
                    "lhs_key": expr_dict["lhs"]["value"],
                    "rhs_embed": expr_dict["rhs"],
                }

        # If it's a vector sort, we will handle it later. If not, use existing logic.
        if not is_vector_sort:
            for i, (sort_key, mode) in enumerate(sort_dict.items()):
                if is_image_field(sort_key, field_types) or is_audio_field(
                    sort_key,
                    field_types,
                ):
                    continue
                if mode not in ("ascending", "descending"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Sort mode must be 'ascending' or 'descending', got {mode}.",
                    )

                # Parse expression
                try:
                    expr_dict = str_filter_exp_to_dict(
                        sort_key,
                        field_names=list(field_types.keys()),
                    )
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid sort expression '{sort_key}'",
                    )

                if expr_dict.get("type", None) == "identifier":
                    # static field sorting
                    # build a *key‑specific* unified view – orders of magnitude smaller
                    key_ul = _build_unified_logs_subquery(
                        session=session,
                        relevant_log_events=relevant_log_events,
                        key=sort_key,  # ❷  filter at source
                    )

                    cast_expr = _build_sort_criteria(
                        key_ul.c.value,
                        sort_key,
                        field_types,
                    )
                    agg_target = (
                        cast(cast_expr, Text)
                        if isinstance(cast_expr.type, JSONB)
                        else cast_expr
                    )

                    sort_val_sq = (
                        select(
                            key_ul.c.log_event_id.label("log_event_id"),
                            agg_target.label(
                                "val",
                            ),  # ← same typed value you had before
                        )
                        .distinct(key_ul.c.log_event_id)  # DISTINCT ON(log_event_id)
                        .order_by(key_ul.c.log_event_id)  # walks the new index
                        .subquery(f"sort_{sort_key}_sq")
                    )

                    sort_val_sqs.append(sort_val_sq)

                    # remember ORDER‑BY expression
                    direction = asc if mode == "ascending" else desc
                    sort_criteria.append(direction(sort_val_sq.c.val).nulls_last())

                else:
                    # dynamic expression sorting
                    event_ids_subq = log_event_query.subquery(name="event_ids_subq")
                    sort_expr = build_sql_query(
                        expr_dict,
                        LogEvent,
                        session,
                        log_event_ids=event_ids_subq,
                    )
                    rand = random.randint(1, 1000000)
                    base_sq = sort_expr.alias(f"sort_base_{rand}")
                    sort_val_sq = (
                        select(
                            base_sq.c.log_event_id.label("log_event_id"),
                            base_sq.c.value.label("val"),
                        )
                        .where(base_sq.c.log_event_id.in_(select(event_ids_subq.c.id)))
                        .subquery(f"sort_expr_{rand}")
                    )

                    sort_val_sqs.append(sort_val_sq)

                    # Add to ORDER BY clauses
                    direction = asc if mode == "ascending" else desc
                    sort_criteria.append(direction(sort_val_sq.c.val).nulls_last())

    # Return the flag and details so the calling function can decide which query path to take.
    return is_vector_sort, vector_sort_details


def _apply_post_filters(
    base_q,
    ul_table,
    from_ids,
    exclude_ids,
    from_fields,
    exclude_fields,
    exclude_params,
    exclude_entries,
):
    # Validate ID filters
    if from_ids and exclude_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )

    # Apply ID filters
    if from_ids:
        include_ids = [int(x) for x in from_ids.split("&")]
        base_q = base_q.filter(
            ul_table.c.log_event_id.in_(include_ids),
        )
    elif exclude_ids:
        exclude_set = [int(x) for x in exclude_ids.split("&")]
        base_q = base_q.filter(
            ul_table.c.log_event_id.notin_(exclude_set),
        )

    # Apply param/entry type filters
    if exclude_params:
        base_q = base_q.filter(
            ul_table.c.param_version.is_(None),
        )
    elif exclude_entries:
        base_q = base_q.filter(
            ul_table.c.param_version.isnot(None),
        )

    # Validate field filters
    if from_fields and exclude_fields:
        raise HTTPException(
            status_code=400,
            detail="Only one of from_fields or exclude_fields can be set.",
        )

    # Apply field filters
    if from_fields:
        allowed_fields = from_fields.split("&")
        base_q = base_q.filter(
            ul_table.c.key.in_(allowed_fields),
        )
    elif exclude_fields:
        excluded_fields = exclude_fields.split("&")
        base_q = base_q.filter(
            ul_table.c.key.notin_(excluded_fields),
        )

    return base_q


def flatten_and_conditions(filter_dict):
    """Recursively flattens a nested 'and' filter dict into a list of conditions."""
    conditions = []
    if isinstance(filter_dict, dict) and filter_dict.get("operand") == "and":
        conditions.extend(flatten_and_conditions(filter_dict.get("lhs")))
        conditions.extend(flatten_and_conditions(filter_dict.get("rhs")))
    elif filter_dict:
        conditions.append(filter_dict)
    return conditions


def flatten_or_conditions(filter_dict):
    """Recursively flattens a nested 'or' filter dict into a list of conditions."""
    conditions = []
    if isinstance(filter_dict, dict) and filter_dict.get("operand") == "or":
        conditions.extend(flatten_or_conditions(filter_dict.get("lhs")))
        conditions.extend(flatten_or_conditions(filter_dict.get("rhs")))
    elif filter_dict:
        conditions.append(filter_dict)
    return conditions


# NOTE: The legacy _get_logs_query implementation has been removed.
# The version below is the only implementation.


def _get_logs_query(
    request_fastapi: Request,
    project_name: str,
    context: Optional[str],
    filter_expr: Optional[str],
    sorting: Optional[str],
    from_ids: Optional[Any],
    exclude_ids: Optional[Any],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    limit: Optional[int],
    offset: int,
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session=Depends(get_db_session),
    latest_timestamp: bool = False,
    randomize: bool = False,
    seed: Optional[str] = "42",
) -> tuple:
    """
    JSONB-based query function for retrieving logs.

    JSONB-based query function for retrieving logs. Provides the same filtering capabilities and
    leverages JSONB's performance benefits by querying the LogEvent.data column directly.

    Key characteristics:
    - Data is stored in LogEvent.data JSONB column
    - Simpler query structure with better performance
    - Returns (rows, total_count) instead of (results, context_len, total_count)

    Args:
        request_fastapi: The FastAPI request object containing user info
        project_name: Project name to filter logs for
        context: Optional context name to filter logs within
        filter_expr: Optional filter expression string (Python-like syntax)
        sorting: Optional JSON string specifying sort order, e.g. '{"field": "ascending"}'
        from_ids: Optional ampersand-separated list of log event IDs to include
        exclude_ids: Optional ampersand-separated list of log event IDs to exclude
        from_fields: Optional ampersand-separated list of fields - include logs with any of these
        exclude_fields: Optional ampersand-separated list of fields - exclude logs with these
        limit: Optional maximum number of results to return
        offset: Number of results to skip (pagination)
        project_dao: Data access object for projects
        field_type_dao: Data access object for field types
        context_dao: Data access object for contexts
        session: Database session
        latest_timestamp: If True, return only the latest created_at timestamp as ISO string
        randomize: If True, return logs in deterministic random order instead of newest-first
        seed: Seed value for deterministic random ordering (default "42")

    Returns:
        If latest_timestamp is True: ISO formatted string of the latest created_at timestamp
        Otherwise: Tuple of (rows, total_count) where:
        - rows: List of tuples (id, data, created_at) for each matching log event
        - total_count: Total number of matching events before pagination

    Raises:
        HTTPException 404: If project or context not found
        HTTPException 400: If filter expression is invalid or mutually exclusive params used
    """
    import time

    start_time = time.time()
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # =========================================================================
    # STEP 1: Validate project
    # =========================================================================
    try:
        project_id = project_dao.get_by_user_and_name(
            name=project_name,
            user_id=user_id,
            organization_id=organization_id,
        ).id
    except (IndexError, AttributeError):
        raise not_found(f"Project {project_name}")

    # =========================================================================
    # STEP 2: Build base query (SELECT id, data, key_order, created_at FROM log_event)
    # =========================================================================
    query = session.query(
        LogEvent.id,
        LogEvent.data,
        LogEvent.key_order,
        LogEvent.created_at,
    ).filter(
        LogEvent.project_id == project_id,
    )

    # =========================================================================
    # STEP 3: Apply context filter
    # =========================================================================
    context_id = None
    if context:
        context_obj = context_dao.filter(name=context, project_id=project_id)
        if not context_obj:
            raise HTTPException(
                status_code=404,
                detail=f"Context '{context}' not found",
            )
        context_id = context_obj[0][0].id
        query = query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )
    else:
        # Get the default context (empty string name)
        context_obj = context_dao.filter(name="", project_id=project_id)
        if context_obj:
            context_id = context_obj[0][0].id
            # Also filter by context membership for default context
            # This ensures logs removed from default context aren't returned
            query = query.join(LogEventContext).filter(
                LogEventContext.context_id == context_id,
            )
        else:
            # No default context exists - return empty results
            # Logs are always bound to a context
            return [], 0

    # =========================================================================
    # STEP 4: Get field types for validation and type casting
    # =========================================================================
    field_types = field_type_dao.get_field_types(project_id, context_id=context_id)

    # =========================================================================
    # STEP 5: Apply filter expression (Python-like syntax → SQL WHERE clause)
    # =========================================================================
    if filter_expr:
        try:
            filter_dict = str_filter_exp_to_dict(
                filter_expr,
                field_names=list(field_types.keys()),
            )
        except Exception as e:
            session.rollback()
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filter expression: {str(e)}",
            )

        if filter_dict:
            # Validate media field usage
            def validate_filter_dict(fd):
                if isinstance(fd, dict):
                    if "type" in fd and fd["type"] == "identifier":
                        field = fd.get("value")
                        if is_image_field(field, field_types) or is_audio_field(
                            field,
                            field_types,
                        ):
                            parent = getattr(validate_filter_dict, "parent", None)
                            if parent and parent.get("operand") not in (
                                "exists",
                                "isNone",
                                "phash",
                                "phash_distance",
                            ):
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Field '{field}' is a media type and can only be used with 'exists' or 'isNone' or 'phash' or 'phash_distance' operator",
                                )
                    for k, v in fd.items():
                        if isinstance(v, dict):
                            validate_filter_dict.parent = fd
                            validate_filter_dict(v)

            try:
                validate_filter_dict(filter_dict)

                # Build SQL condition using JSONB query builder
                # Apply CTE optimization for aggregation functions if enabled
                enable_cte = getattr(
                    settings,
                    "use_aggregation_cte_optimization",
                    False,
                )

                # Create a subquery of the current filtered log event IDs
                filter_event_ids_subq = query.with_entities(LogEvent.id).subquery(
                    name="filter_event_ids",
                )

                condition_result = build_sql_query(
                    filter_dict,
                    LogEvent,
                    session,
                    log_event_ids=filter_event_ids_subq,
                    project_id=project_id,
                    context_id=context_id,
                    enable_cte_optimization=enable_cte,
                )

                # Check if CTEs were generated (tuple returned)
                query_context = None
                if isinstance(condition_result, tuple):
                    condition_sql, query_context = condition_result
                else:
                    condition_sql = condition_result

                # Apply CTE optimization if CTEs were generated
                if query_context is not None and query_context.has_aggregations():
                    # Build base filters for CTE (project_id, context_id)
                    base_filters = [LogEvent.project_id == project_id]
                    if context_id:
                        base_filters.append(
                            LogEvent.id.in_(
                                select(LogEventContext.log_event_id).where(
                                    LogEventContext.context_id == context_id,
                                ),
                            ),
                        )

                    # Generate CTE objects
                    cte_objects = query_context.build_ctes(
                        log_event_alias=LogEvent,
                        base_filters=base_filters,
                    )

                    # Replace CTE column references in WHERE clause
                    condition_sql = query_context.replace_cte_refs(
                        condition_sql,
                        cte_objects,
                    )

                    # Join CTEs to the main query
                    for cte_name, cte in cte_objects.items():
                        query = query.join(cte, LogEvent.id == cte.c.id)

                # Apply the filter condition
                if isinstance(condition_sql, Subquery):
                    # If build_sql_query returned a subquery, filter by log_event_id
                    # where the value column is truthy (True for boolean, non-null for others)
                    truthiness_clause = _create_truthiness_condition(
                        condition_sql,
                        session,
                    )
                    query = query.filter(
                        LogEvent.id.in_(
                            select(condition_sql.c.log_event_id).where(
                                truthiness_clause,
                            ),
                        ),
                    )
                else:
                    # Direct expression - ensure it's a boolean condition.
                    # JSONB expressions must be converted to boolean for PostgreSQL's
                    # WHERE clause. This handles patterns like `metadata.get('key')`
                    # used as standalone filter conditions.
                    from orchestra.web.api.log.python2SQL.jsonb_builder import (
                        _create_truthiness_condition_jsonb,
                    )

                    condition_sql = _create_truthiness_condition_jsonb(
                        condition_sql,
                        session,
                        project_id,
                        context_id,
                    )
                    query = query.filter(condition_sql)

            except HTTPException:
                raise
            except Exception as e:
                session.rollback()
                error_msg = f"Error processing filter expression: {str(e)}"
                if hasattr(e, "__class__"):
                    error_msg = f"{e.__class__.__name__}: {error_msg}"
                raise HTTPException(
                    status_code=400,
                    detail=error_msg,
                )

    # =========================================================================
    # STEP 6: Apply ID inclusion/exclusion filters
    # =========================================================================
    if from_ids and exclude_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )

    if from_ids:
        include_ids = [int(x) for x in from_ids.split("&")]
        query = query.filter(LogEvent.id.in_(include_ids))
    elif exclude_ids:
        exclude_set = [int(x) for x in exclude_ids.split("&")]
        query = query.filter(LogEvent.id.notin_(exclude_set))

    # =========================================================================
    # STEP 7: Apply field existence filters (JSONB ? operator)
    # =========================================================================
    if from_fields and exclude_fields:
        raise HTTPException(
            status_code=400,
            detail="Only one of from_fields or exclude_fields can be set.",
        )

    if from_fields:
        # Filter to include log events that have at least one of the specified fields
        allowed_fields = from_fields.split("&")
        # Use JSONB has_key (? operator) with OR logic for data fields
        or_conditions = [LogEvent.data.has_key(field) for field in allowed_fields]
        # Also check Embedding table for vector fields (embeddings are stored separately)
        # Exclude soft-deleted embeddings from existence checks
        embedding_exists = (
            session.query(Embedding.ref_id)
            .filter(
                Embedding.ref_id == LogEvent.id,
                Embedding.key.in_(allowed_fields),
                Embedding.is_deleted
                == False,  # noqa: E712 - SQLAlchemy requires == for SQL generation
            )
            .exists()
        )
        or_conditions.append(embedding_exists)
        query = query.filter(or_(*or_conditions))
    # Note: exclude_fields is NOT applied at the query level in JSONB mode.
    # It's applied at the formatting level in _format_logs to filter
    # which fields are returned, not which log events are returned.
    # exclude_fields filters keys, not log events.

    # =========================================================================
    # STEP 8: Build sorting expressions (standard or vector ANN)
    # =========================================================================
    order_by_cols = []
    has_sort_joins = False

    # Create a subquery of the current filtered log event IDs for use in sorting expressions
    filtered_event_ids_subq = query.with_entities(LogEvent.id).subquery(
        name="filtered_event_ids_for_sort",
    )

    if sorting:
        try:
            sort_dict = json.loads(sorting)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid sorting JSON: {str(e)}",
            )

        if not isinstance(sort_dict, dict):
            raise HTTPException(
                status_code=400,
                detail="Sorting must be a JSON object mapping field names to 'ascending' or 'descending'",
            )

        # Check for vector sort pattern (single key with cosine/l2/ip operand)
        # Vector sorts are handled in a separate phase - skip for now
        is_vector_sort = False
        if len(sort_dict) == 1:
            sort_key, mode = next(iter(sort_dict.items()))
            try:
                expr_dict = str_filter_exp_to_dict(
                    sort_key,
                    field_names=list(field_types.keys()),
                )
                if (
                    isinstance(expr_dict, dict)
                    and expr_dict.get("operand") in ("cosine", "l2", "ip")
                    and isinstance(expr_dict.get("lhs"), dict)
                    and isinstance(expr_dict.get("rhs"), dict)
                ):
                    # Check if LHS is either:
                    # 1. A simple identifier (field name)
                    # 2. An embed/embed_image call (which wraps a field)
                    lhs = expr_dict["lhs"]
                    lhs_is_valid = lhs.get("type") == "identifier" or lhs.get(
                        "operand",
                    ) in ("embed", "embed_image")
                    # RHS must be an embed/embed_image call
                    rhs_is_valid = expr_dict["rhs"].get("operand") in (
                        "embed",
                        "embed_image",
                    )

                    if lhs_is_valid and rhs_is_valid:
                        is_vector_sort = True
            except Exception:
                pass  # Not parseable, not a vector sort

        # Handle vector sort vs standard sort
        if is_vector_sort:
            # --- Vector ANN fast-path ---
            # This mirrors the existing vector sorting implementation
            try:
                # Extract vector sort details from the parsed expression
                sort_key, mode = next(iter(sort_dict.items()))
                expr_dict = str_filter_exp_to_dict(
                    sort_key,
                    field_names=list(field_types.keys()),
                )

                operand = expr_dict["operand"]  # "cosine" | "l2" | "ip"
                lhs = expr_dict["lhs"]
                rhs_embed = expr_dict["rhs"]  # embed() or embed_image() dict

                # Extract the field key from LHS
                # LHS can be either:
                # 1. A simple identifier: {"type": "identifier", "value": "field_name"}
                # 2. An embed call: {"operand": "embed", "rhs": [{"type": "identifier", "value": "field_name"}]}
                if lhs.get("type") == "identifier":
                    lhs_key = lhs["value"]
                elif lhs.get("operand") in ("embed", "embed_image"):
                    # Extract field from embed() argument
                    embed_args = lhs.get("rhs", [])
                    if (
                        embed_args
                        and isinstance(embed_args[0], dict)
                        and embed_args[0].get("type") == "identifier"
                    ):
                        lhs_key = embed_args[0]["value"]
                    else:
                        raise ValueError("Cannot extract field key from embed() call")
                else:
                    raise ValueError("Unexpected LHS structure in vector sort")

                # Build RHS vector using JSONB query builder
                # This handles both literal text and field references
                rhs_sql = build_sql_query(
                    rhs_embed,
                    LogEvent,
                    session,
                    log_event_ids=filtered_event_ids_subq,
                    project_id=project_id,
                    context_id=context_id,
                )

                # Extract vector value using helper
                rhs_vec, _ = _select_value(
                    rhs_sql,
                    session,
                    is_vector=True,
                    project_id=project_id,
                    context_id=context_id,
                )

                # Query Embedding table to detect model and dimension
                embedding_model_query = session.execute(
                    select(Embedding.model).where(Embedding.key == lhs_key).limit(1),
                ).scalar()

                # Map model to dimension for HNSW index usage
                model_to_dim = {
                    "text-embedding-3-small": 1536,
                    "multimodalembedding@001": 1408,
                }
                embedding_dim = model_to_dim.get(embedding_model_query, None)

                # Choose pgvector distance operator
                op = {"cosine": "<=>", "l2": "<->", "ip": "<#>"}[operand]

                # Cast vector to correct dimension to use HNSW index
                # This is critical for PostgreSQL to use the model-specific partial indexes
                if embedding_dim and embedding_model_query:
                    casted_vector = func.cast(Embedding.vector, Vector(embedding_dim))
                    dist = casted_vector.op(op)(rhs_vec)
                    model_filter = Embedding.model == embedding_model_query
                else:
                    # Fallback without cast (slower, no index)
                    dist = Embedding.vector.op(op)(rhs_vec)
                    model_filter = literal(True)

                # Determine sort direction
                asc_sort = mode == "ascending"

                # Calculate total count before vector query
                count_query = query.with_entities(func.count(LogEvent.id))
                total_count = count_query.scalar()

                # Implement top-K optimization on Embedding table
                # Apply LIMIT before pagination to leverage HNSW index
                top_k = (offset or 0) + (limit or 100)

                # Get filtered event IDs from base query
                filtered_event_ids_subq = query.with_entities(LogEvent.id).subquery(
                    "filtered_events",
                )

                ann_topk = (
                    select(
                        Embedding.ref_id.label("id"),
                        dist.label("dist"),
                    )
                    .where(
                        Embedding.key == lhs_key,
                        model_filter,
                        Embedding.is_deleted == False,  # noqa: E712
                        Embedding.vector.isnot(None),
                        Embedding.ref_id.in_(select(filtered_event_ids_subq.c.id)),
                    )
                    .order_by(
                        dist.asc() if asc_sort else dist.desc(),
                        Embedding.ref_id.desc(),
                    )
                    .limit(top_k)
                    .subquery("ann_topk")
                )

                # Paginate the top-K results
                row_order = [
                    ann_topk.c.dist.asc() if asc_sort else ann_topk.c.dist.desc(),
                    ann_topk.c.id.desc(),
                ]

                paginated_ids_subq = select(
                    ann_topk.c.id,
                    func.row_number().over(order_by=row_order).label("row_num"),
                ).order_by(*row_order)

                if offset > 0:
                    paginated_ids_subq = paginated_ids_subq.offset(offset)
                if limit:
                    paginated_ids_subq = paginated_ids_subq.limit(limit)

                paginated_ids_cte = paginated_ids_subq.cte("paginated_ids").prefix_with(
                    "MATERIALIZED",
                )

                # Fetch final results with data and created_at
                # Join with paginated_ids_cte to preserve correct ordering via row_num
                final_query = (
                    session.query(LogEvent.id, LogEvent.data, LogEvent.created_at)
                    .join(paginated_ids_cte, LogEvent.id == paginated_ids_cte.c.id)
                    .order_by(paginated_ids_cte.c.row_num)
                )

                rows = final_query.all()

                # Return results in standard format
                return (rows, total_count)

            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Error processing vector sort: {str(e)}",
                )

        else:
            # --- Standard sorting path ---
            for sort_key, mode in sort_dict.items():
                # Skip media fields (image/audio) - they can't be meaningfully sorted
                if is_image_field(sort_key, field_types) or is_audio_field(
                    sort_key,
                    field_types,
                ):
                    continue

                # Validate sort mode
                if mode not in ("ascending", "descending"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Sort mode must be 'ascending' or 'descending', got '{mode}'",
                    )

                # Parse the sort key expression
                try:
                    expr_dict = str_filter_exp_to_dict(
                        sort_key,
                        field_names=list(field_types.keys()),
                    )
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid sort expression '{sort_key}'",
                    )

                direction = asc if mode == "ascending" else desc

                if expr_dict.get("type") == "identifier":
                    # Static field sorting - use direct JSONB extraction
                    # Data is stored flat in LogEvent.data, e.g., data->>'field_name'
                    field_name = expr_dict["value"]

                    # Get the field type for proper casting
                    # field_types can be either {field_name: "type"} or {field_name: {"field_type": "type", ...}}
                    field_meta = field_types.get(field_name)
                    if isinstance(field_meta, dict):
                        raw_pytype = field_meta.get("field_type")
                    else:
                        raw_pytype = field_meta
                    # Normalize type to SQL-compatible (handles Pydantic schemas, Optional[T], etc.)
                    from .type_utils import get_sql_casting_type

                    pytype = get_sql_casting_type(raw_pytype) if raw_pytype else None
                    cast_type = STR_TO_SQL_TYPES.get(pytype) if pytype else None

                    if pytype in ("dict", "list"):
                        # For JSONB types, use -> operator (returns JSONB, not text)
                        field_expr = LogEvent.data.op("->")(field_name)
                        sort_expr = field_expr
                    elif cast_type is not None:
                        # Extract as text using ->> operator
                        field_expr = LogEvent.data.op("->>")(field_name)

                        if pytype in ("datetime", "date", "time"):
                            # Handle NULL and 'null' string for datetime types
                            sort_expr = case(
                                (field_expr.is_(None), None),
                                (field_expr == "null", None),
                                else_=cast(field_expr, cast_type),
                            )
                        else:
                            # Handle NULL and 'null' string for other types
                            sort_expr = case(
                                (field_expr.is_(None), None),
                                (field_expr == "null", None),
                                else_=cast(field_expr, cast_type),
                            )
                    else:
                        # No type info - use as text
                        field_expr = LogEvent.data.op("->>")(field_name)
                        sort_expr = field_expr

                    order_by_cols.append(direction(sort_expr).nulls_last())

                else:
                    # Dynamic expression sorting - use build_sql_query
                    try:
                        sort_result = build_sql_query(
                            expr_dict,
                            LogEvent,
                            session,
                            log_event_ids=filtered_event_ids_subq,
                            project_id=project_id,
                            context_id=context_id,
                        )

                        if isinstance(sort_result, Subquery):
                            # Complex expression returned a subquery
                            # We need to join with this subquery for sorting
                            has_sort_joins = True
                            # Use the subquery's value column for sorting
                            sort_expr = sort_result.c.value
                            order_by_cols.append(direction(sort_expr).nulls_last())
                        else:
                            # Direct expression - apply direction
                            order_by_cols.append(direction(sort_result).nulls_last())

                    except Exception as e:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Error building sort expression for '{sort_key}': {str(e)}",
                        )

    # Always add tie-breaker to ensure deterministic ordering
    order_by_cols.append(desc(LogEvent.id))

    # =========================================================================
    # STEP 9: Count total results (before pagination)
    # =========================================================================
    count_query = query.with_entities(func.count(LogEvent.id))
    try:
        total_count = count_query.scalar()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error fetching logs: {str(e)}",
        )

    # =========================================================================
    # STEP 10: Handle latest_timestamp special case (early return)
    # =========================================================================
    if latest_timestamp:
        # Use updated_at to reflect updates - this changes after log updates
        max_updated_at = query.with_entities(func.max(LogEvent.updated_at)).scalar()
        return max_updated_at.isoformat() if max_updated_at else None

    # =========================================================================
    # STEP 11: Apply ordering and pagination
    # =========================================================================
    # PostgreSQL can use ORDER BY + LIMIT/OFFSET indexes and stop early.
    if randomize:
        random_key = func.md5(cast(LogEvent.id, String) + literal(seed))
        order_by_cols = [random_key, desc(LogEvent.id)]

    query = query.order_by(*order_by_cols)

    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)

    rows = query.all()

    # Capture SQL for test analysis (if enabled)
    try:
        from sqlalchemy import text

        from orchestra.tests.test_log.sql_capture import (
            capture_sql,
            is_capture_enabled,
            set_test_context,
        )

        if is_capture_enabled():
            mode = "jsonb"
            # Compile SQL for capture
            compiled_sql = query.statement.compile(
                dialect=session.bind.dialect,
                compile_kwargs={"literal_binds": True},
            ).string
            # Execute EXPLAIN ANALYZE
            explain_sql = (
                "EXPLAIN (ANALYZE, BUFFERS, TIMING, COSTS, VERBOSE, FORMAT JSON) "
                + compiled_sql
            )
            explain_result = session.execute(text(explain_sql))
            explain_output = explain_result.fetchone()[0]
            # Read test name from header (injected by test client)
            test_name = request_fastapi.headers.get("X-Test-Name", "unknown")
            # Set context and capture
            set_test_context(
                test_name=test_name,
                filter_expr=filter_expr,
                mode=mode,
            )
            capture_sql(
                sql=compiled_sql,
                explain_analyze=explain_output,
                filter_expr_override=filter_expr if filter_expr else None,
            )
    except ImportError:
        pass  # sql_capture module not available (production environment)
    except Exception:
        pass  # Silently ignore capture errors

    # =========================================================================
    # STEP 12: Hydrate embeddings from Embedding table
    # =========================================================================
    # Vectors are stored separately to keep LogEvent.data lean (~6KB per vector).
    # We fetch them only for the paginated results using an indexed lookup.
    if rows:
        # 12a. Collect IDs for the current page only
        page_ids = [r.id for r in rows]
        # 12b. Query Embedding table
        stmt = select(
            Embedding.ref_id,
            Embedding.key,
            func.to_jsonb(Embedding.vector).label("vector_list"),
        ).where(Embedding.ref_id.in_(page_ids))

        if from_fields:
            allowed = from_fields.split("&")
            stmt = stmt.where(Embedding.key.in_(allowed))
        if exclude_fields:
            excluded = exclude_fields.split("&")
            stmt = stmt.where(Embedding.key.notin_(excluded))

        embeddings = session.execute(stmt).fetchall()
        # 12c. Merge embeddings into data payload (without overwriting existing keys)
        if embeddings:
            # Build map: log_event_id → {key: vector_list}
            emb_map = {}
            for ref_id, key, vector_list in embeddings:
                if ref_id not in emb_map:
                    emb_map[ref_id] = {}
                # vector_list from to_jsonb() may come as string in some drivers
                # Parse it as JSON if needed to get a proper Python list
                if isinstance(vector_list, str):
                    try:
                        vector_list = json.loads(vector_list)
                    except (json.JSONDecodeError, TypeError):
                        pass  # Keep as string if parsing fails
                emb_map[ref_id][key] = vector_list

            # 12d. Reconstruct rows with enriched data (Row objects are immutable)
            enriched_rows = []
            for r in rows:
                if r.id in emb_map:
                    merged_data = dict(r.data) if r.data else {}
                    # Add embedding keys, but ONLY overwrite if:
                    # - The key doesn't exist in data
                    # - The key exists but value is None (placeholder)
                    # - The value looks like a serialized vector (list-like string starting with '[')
                    # DO NOT overwrite actual text strings with embeddings, as this would
                    # replace original field values with embeddings stored under the same key.
                    for emb_key, emb_val in emb_map[r.id].items():
                        existing_val = merged_data.get(emb_key)
                        should_add_embedding = (
                            existing_val is None  # No existing value or explicit None
                            or (
                                isinstance(existing_val, str)
                                and existing_val.startswith("[")
                            )  # Serialized vector
                        )
                        if should_add_embedding:
                            merged_data[emb_key] = emb_val
                    enriched_rows.append((r.id, merged_data, r.created_at))
                else:
                    enriched_rows.append(r)

            rows = enriched_rows

    return (rows, total_count)


def _create_logs_internal(
    entries_list: list,
    entries_len: int,
    total_logs: int,
    project_id: int,
    context_id: int,
    context_obj: Optional[Context],
    field_types: dict,
    field_type_dao: FieldTypeDAO,
    log_event_dao: LogEventDAO,
    context_dao: ContextDAO,
) -> dict:
    """
    JSONB-based log creation implementation.

    This function stores all log data (entries, auto-counting fields) in a single
    LogEvent.data JSONB column.
    - Single JSON object: All fields (entries, auto-counting) are stored flat

    Args:
        entries_list: List of entry dictionaries
        entries_len: Length of entries_list
        total_logs: Total number of logs to create
        project_id: The project ID
        context_id: The context ID
        context_obj: The context object (may be None)
        field_types: Existing field types for the project/context
        field_type_dao: Data access object for field types
        log_event_dao: Data access object for log events
        context_dao: Data access object for contexts

    Returns:
        Dict with log_event_ids, row_ids, auto_counting, and failed
    """
    provided_unique_ids = None

    # Handle auto-counting fields (both in unique_keys and not)
    # Reuse the same logic for extracting auto-counting/unique key values
    if context_obj and (context_obj.unique_keys or context_obj.auto_counting):
        unique_keys = context_obj.unique_keys or {}
        auto_counting = context_obj.auto_counting or {}

        # 1. Extract and validate composite key values from entries
        all_composite_values = []
        for i in range(total_logs):
            current_entries = entries_list[min(i, len(entries_list) - 1)] or {}

            # Extract values for composite key columns
            composite_values = {}
            provided_counting_values = {}

            # First process unique key columns
            for col_name, col_type in unique_keys.items():
                if col_name in auto_counting:
                    # For auto-counting columns, check if user provided a value
                    if col_name in current_entries:
                        provided_counting_values[col_name] = current_entries[col_name]
                else:
                    # Non-auto-counting columns must be provided
                    if col_name not in current_entries:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Must provide value for composite key column '{col_name}' (type: {col_type}).",
                        )
                    composite_values[col_name] = current_entries[col_name]

            # Then process auto-counting columns that are NOT in unique keys
            for col_name, parent_col in auto_counting.items():
                if col_name not in unique_keys and col_name in current_entries:
                    provided_counting_values[col_name] = current_entries[col_name]

            # Validate auto-counting columns follow rules
            if auto_counting and provided_counting_values:
                # For hierarchical counters, validate parent-child relationships
                for col_name, value in provided_counting_values.items():
                    if col_name in auto_counting:
                        parent_col = auto_counting.get(col_name)
                        if (
                            parent_col
                            and parent_col not in composite_values
                            and parent_col not in provided_counting_values
                        ):
                            raise HTTPException(
                                status_code=400,
                                detail=f"Cannot provide value for '{col_name}' without providing parent column '{parent_col}'.",
                            )

                # Add provided counting values to composite values
                composite_values.update(provided_counting_values)

            all_composite_values.append(composite_values)

        # 2. Pop composite key columns from original entries to prevent duplication
        for i in range(total_logs):
            current_entries = entries_list[min(i, len(entries_list) - 1)]
            composite_values = all_composite_values[i]

            # Remove composite key columns from entries
            if current_entries:
                for key in unique_keys.keys():
                    current_entries.pop(key, None)

        # 3. Construct the `provided_unique_ids` list for the DAO
        provided_unique_ids = all_composite_values

    # Bulk create all log events in one operation
    log_event_ids, row_ids = log_event_dao.bulk_create(
        project_id=project_id,
        context_id=context_id,
        count=total_logs,
        return_row_ids=True,
        provided_unique_ids=provided_unique_ids,
    )

    # Prepare collections for bulk operations
    new_field_types = []
    failed_logs = []
    successful_indices = []
    log_data_updates = []  # List of (log_event_id, data_dict) tuples

    # Process all logs in the batch
    for i in range(total_logs):
        log_event_id = log_event_ids[i]

        # Per-log staging to isolate failures
        perlog_field_types = []

        # Get current entries (clone to avoid in-place mutations leaking)
        current_entries = dict(entries_list[min(i, entries_len - 1)] or {})

        try:
            # Add auto-incremented values from row_ids that are not in unique_keys back to entries
            if (
                context_obj
                and context_obj.auto_counting
                and row_ids
                and i < len(row_ids)
            ):
                row_id_dict = row_ids[i] if isinstance(row_ids[i], dict) else {}
                unique_keys = context_obj.unique_keys or {}

                for col_name, col_value in row_id_dict.items():
                    # Only add if it's an auto-counting field that's NOT in unique_keys
                    # (unique_key fields are already handled by log_event_dao.bulk_create)
                    if (
                        col_name in context_obj.auto_counting
                        and col_name not in unique_keys
                    ):
                        if col_name not in current_entries:
                            # Add to entries
                            current_entries[col_name] = col_value

            # Extract explicit types - NOTE: This mutates entries dict in-place
            entries_explicit_types = (
                current_entries.pop("explicit_types", {})
                if isinstance(current_entries, dict)
                else None
            )

            # Extract infer_untyped_fields flag from entries
            # If True, fields with type "Any" will have their type inferred from values
            infer_untyped_fields = (
                current_entries.pop("infer_untyped_fields", False)
                if isinstance(current_entries, dict)
                else False
            )

            # Use entries explicit types
            merged_explicit_types = entries_explicit_types or {}

            # Process entries - create new fields if they don't exist
            for k, v in current_entries.items():
                # Check if field needs to be created
                if k not in field_types:
                    mutable = (
                        entries_explicit_types.get(k, {}).get("mutable", True)
                        if entries_explicit_types
                        else True
                    )
                    unique = (
                        entries_explicit_types.get(k, {}).get("unique", False)
                        if entries_explicit_types
                        else False
                    )
                    # If in a versioned context, force mutable=True
                    if context_obj and context_obj.is_versioned:
                        mutable = True

                    # Check for explicit type
                    field_type = None
                    enum_values = None
                    enum_restrict = False
                    if entries_explicit_types and k in entries_explicit_types:
                        field_spec = entries_explicit_types[k]
                        if isinstance(field_spec, dict):
                            field_type = field_spec.get("type")
                            enum_values = field_spec.get("values")
                            enum_restrict = field_spec.get("restrict", False)
                        elif isinstance(field_spec, str):
                            field_type = field_spec

                    # Validate value against Pydantic schema BEFORE creating the field
                    if field_type is not None:
                        from orchestra.web.api.log.utils.type_utils import (
                            is_pydantic_schema,
                            normalize_pydantic_schema,
                            validate_value_against_pydantic_schema,
                        )

                        if is_pydantic_schema(field_type):
                            try:
                                schema = normalize_pydantic_schema(field_type)
                                ok, err = validate_value_against_pydantic_schema(
                                    v,
                                    schema,
                                )
                            except Exception as e:
                                ok, err = (False, str(e))
                            if not ok:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Type validation against explicit schema failed for '{k}' (in batch entry {i}): {err}",
                                )

                    perlog_field_types.append(
                        {
                            "project_id": project_id,
                            "field_name": k,
                            "value": v,
                            "mutable": mutable,
                            "unique": unique,
                            "field_category": "entry",
                            "context_id": context_id,
                            "field_type": field_type,
                            "enum_values": enum_values,
                            "enum_restrict": enum_restrict,
                        },
                    )
                else:
                    # Field exists - check if we should infer type for untyped fields
                    from orchestra.web.api.log.utils.type_utils import is_untyped_field

                    field_info = field_types.get(k, {})
                    current_field_type = field_info.get("field_type", "Any")

                    if infer_untyped_fields and is_untyped_field(current_field_type):
                        # Infer type from value and update the field
                        inferred_type = LogEventDAO.infer_type(k, v, explicit_type=None)
                        updated = field_type_dao.update_untyped_field_to_inferred(
                            project_id,
                            k,
                            context_id,
                            inferred_type,
                        )
                        if updated:
                            # Update local cache so subsequent logs see the new type
                            field_types[k]["field_type"] = inferred_type
                    else:
                        # Normal path: enforce types (cannot modify existing field types)
                        enforce_types(
                            k,
                            v,
                            field_types=field_types,
                            field_type_dao=field_type_dao,
                            context_dao=context_dao,
                            project_id=project_id,
                            batch_index=i,
                            explicit_types=entries_explicit_types,
                            context_id=context_id,
                            is_param=False,
                        )

            # Build log_data dictionary from entries
            log_data = {}

            # Add entries
            for k, v in current_entries.items():
                log_data[k] = v

            # Inject auto-counting values from row_ids into log_data
            if row_ids and i < len(row_ids):
                row_id_dict = row_ids[i] if isinstance(row_ids[i], dict) else {}
                for col_name, col_value in row_id_dict.items():
                    # Include all auto-counting values (both unique key and non-unique key)
                    log_data[col_name] = col_value

            # Extract key ordering from the original data before PostgreSQL alphabetizes it
            # This preserves the insertion order for nested dictionaries
            key_order = extract_key_order(log_data)

            # If we made it here, this log is valid; stage its artifacts
            new_field_types.extend(perlog_field_types)
            successful_indices.append(i)
            log_data_updates.append((log_event_id, log_data, key_order))

        except HTTPException as http_err:
            try:
                log_event_dao.delete(log_event_id)
            except Exception:
                pass
            failed_logs.append(
                {
                    "index": i,
                    "error": getattr(http_err, "detail", str(http_err)),
                },
            )
            continue
        except Exception as e:  # Broad catch to avoid aborting the batch
            try:
                log_event_dao.delete(log_event_id)
            except Exception:
                pass
            failed_logs.append({"index": i, "error": str(e)})
            continue

    # Bulk create new field types if any
    try:
        if new_field_types:
            field_type_dao.bulk_create_field_types(new_field_types)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # =========================================================================
    # UNIQUE FIELD VALIDATION
    # Uses lookup table (O(M×log N)) or JSONB scan (O(N×M)) based on config.
    # Controlled by ORCHESTRA_UNIQUE_VALIDATION_MODE environment variable.
    # =========================================================================
    if log_data_updates:
        from orchestra.db.dao.unique_constraint_dao import UniqueConstraintDAO

        session = log_event_dao.session

        # Get unique fields from EXISTING field types
        # field_types structure: {"field_name": {"field_type": "str", "unique": True}}
        unique_fields = {
            k
            for k, v in field_types.items()
            if isinstance(v, dict) and v.get("unique", False)
        }

        # ALSO include unique fields from NEWLY created field types (via explicit_types)
        # This handles the case where a log creates a new unique field in the same request
        if new_field_types:
            new_unique_fields = {
                ft["field_name"] for ft in new_field_types if ft.get("unique", False)
            }
            unique_fields = unique_fields | new_unique_fields

        if unique_fields:
            unique_dao = UniqueConstraintDAO(session)

            # Prepare log entries for batch validation
            log_entries = [
                (log_event_id, log_data)
                for log_event_id, log_data, _ in log_data_updates
            ]

            # Exclude newly created log_event_ids from duplicate check
            new_log_ids = [log_event_id for log_event_id, _, _ in log_data_updates]

            # Check for duplicates (handles both lookup table and JSONB scan)
            duplicate = unique_dao.check_unique_fields_batch(
                context_id=context_id,
                project_id=project_id,
                log_entries=log_entries,
                unique_fields=unique_fields,
                exclude_ids=new_log_ids,
            )

            if duplicate:
                dup_log_id, field_name, _ = duplicate

                # Clean up: remove constraints for all new logs
                unique_dao.remove_constraints_for_logs(new_log_ids)

                # Delete all the log events we just created
                for log_event_id in new_log_ids:
                    try:
                        log_event_dao.delete(log_event_id)
                    except Exception:
                        pass

                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate entry for unique field '{field_name}'.",
                )

    # Batch update LogEvent.data and key_order columns for all successful logs
    created_event_ids = [log_event_ids[i] for i in successful_indices]
    if log_data_updates:
        try:
            # Get the session from the DAO
            session = log_event_dao.session

            # =========================================================================
            # BULK UPDATE: Use single UPDATE with parameterized arrays for O(1) roundtrip
            # This issues one SQL statement regardless of batch size and safely handles
            # special characters in JSON payloads via parameter binding
            # =========================================================================
            ids_array = [log_event_id for log_event_id, _, _ in log_data_updates]
            data_array = [json.dumps(log_data) for _, log_data, _ in log_data_updates]
            key_order_array = [
                json.dumps(key_order) for _, _, key_order in log_data_updates
            ]

            if ids_array:
                # Use unnest with parameterized arrays - safe from SQL injection
                # Also update key_order to preserve nested dict key ordering
                # NOTE: Do NOT update updated_at here - this is initial creation, not modification
                # updated_at should only change when logs are actually updated via PUT
                update_sql = text(
                    """
                    UPDATE log_event
                    SET data = v.new_data::jsonb,
                        key_order = v.new_key_order::jsonb
                    FROM unnest(:ids, :data, :key_orders) AS v(id, new_data, new_key_order)
                    WHERE log_event.id = v.id
                """,
                )
                session.execute(
                    update_sql,
                    {
                        "ids": ids_array,
                        "data": data_array,
                        "key_orders": key_order_array,
                    },
                )
                session.flush()
        except Exception as e:
            # On failure, try per-log updates to salvage what we can
            created_event_ids = []
            for idx, (log_event_id, log_data, key_order) in enumerate(log_data_updates):
                original_index = successful_indices[idx]
                try:
                    session.query(LogEvent).filter(LogEvent.id == log_event_id).update(
                        {"data": log_data, "key_order": key_order},
                        synchronize_session=False,
                    )
                    session.flush()
                    created_event_ids.append(log_event_id)
                except Exception as inner_e:
                    try:
                        log_event_dao.delete(log_event_id)
                    except Exception:
                        pass
                    failed_logs.append({"index": original_index, "error": str(inner_e)})

    # =========================================================================
    # BATCH DUPLICATE CHECK: Use JSONB-aware batch duplicate checking (O(1) query)
    # instead of O(N) individual checks
    # =========================================================================
    if context_obj and not context_obj.allow_duplicates and created_event_ids:
        # Use JSONB-aware batch duplicate checking (single SQL query)
        duplicate_ids = context_dao.check_for_duplicates_batch(
            context_obj.id,
            created_event_ids,
        )
        if duplicate_ids:
            # Delete all duplicate logs in one batch
            for dup_id in duplicate_ids:
                try:
                    log_event_dao.delete(dup_id)
                except Exception:
                    pass
                # Remove from created_event_ids
                if dup_id in created_event_ids:
                    created_event_ids.remove(dup_id)
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate log(s) detected in context '{context_obj.name}' which doesn't allow duplicates. Log event IDs: {duplicate_ids}",
            )

    if context_obj and context_obj.is_versioned:
        context_obj.updated_at = datetime.now(timezone.utc)

    # Build row_ids payload (unique key columns only)
    unique_keys = context_obj.unique_keys or {} if context_obj else {}

    names = []
    ids_list = []

    # Transform row_ids into the list format
    # Filter row_ids to only successful ones (preserve order)
    row_ids_filtered = [row_ids[i] for i in successful_indices] if row_ids else []

    if row_ids_filtered and unique_keys:
        # Use the preserved order from context
        names = context_obj.unique_key_names or list(unique_keys.keys())

        if isinstance(row_ids_filtered[0], dict):
            # Dictionary format - convert to list of lists using the correct order
            for row_id in row_ids_filtered:
                ids_list.append([row_id.get(name) for name in names])
        else:
            # Legacy single ID case - wrap each ID in a list
            ids_list = [[row_id] for row_id in row_ids_filtered]

    row_ids_payload = {
        "names": names,
        "ids": ids_list,
    }

    # Build auto_counting payload (all auto-counting columns with their values)
    auto_counting_payload = {}
    auto_counting_cfg = context_obj.auto_counting or {} if context_obj else {}
    if row_ids_filtered and auto_counting_cfg and isinstance(row_ids_filtered[0], dict):
        # Extract auto-counting values as a dict mapping column name to list of values
        for col_name in auto_counting_cfg.keys():
            auto_counting_payload[col_name] = [
                row_id.get(col_name) for row_id in row_ids_filtered
            ]

    return {
        "log_event_ids": created_event_ids,
        "row_ids": row_ids_payload,
        "auto_counting": auto_counting_payload,
        "failed": failed_logs,
    }


def create_logs_internal(
    request: CreateLogConfig,
    project_id: int,
    context_id: int,
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    log_event_dao: LogEventDAO,
    context_dao: ContextDAO,
    context_obj: Context | None = None,
):
    """
    Core implementation of log creation logic, extracted from the create_logs endpoint.
    This function handles the actual creation of logs after project and context validation.

    Args:
        request: The CreateLogConfig containing entries and params to create
        project_id: The ID of the project to create logs for
        context_id: The ID of the context to associate logs with
        project_dao: Data access object for projects
        field_type_dao: Data access object for field types
        log_event_dao: Data access object for log events
        context_dao: Data access object for contexts

    Returns:
        List of created log event IDs

    Raises:
        HTTPException: If validation fails or duplicate logs are detected
    """
    # Convert single entries to list format for uniform processing
    entries_list = (
        request.entries if isinstance(request.entries, list) else [request.entries]
    )

    # Get field types once for all operations
    field_types = field_type_dao.get_field_types(
        project_id,
        return_mutable=True,
        context_id=context_id,
    )

    # Bulk create all log events at once
    entries_len = len(entries_list)
    total_logs = entries_len

    # JSONB mode - all log data is stored in LogEvent.data
    return _create_logs_internal(
        entries_list=entries_list,
        entries_len=entries_len,
        total_logs=total_logs,
        project_id=project_id,
        context_id=context_id,
        context_obj=context_obj,
        field_types=field_types,
        field_type_dao=field_type_dao,
        log_event_dao=log_event_dao,
        context_dao=context_dao,
    )


# TODO(yusha): refactor get_logs_query to make it modular
def _build_unified_logs_subquery(
    session,
    event_ids: Optional[Subquery] = None,
    relevant_log_events: Optional[Subquery] = None,
    key: str = None,
    context_id: Optional[int] = None,
) -> Subquery:
    """
    Build a unified subquery over JSONB log fields.

    Returns rows with:
      - id, log_event_id, key, value, inferred_type
      - param_version/context_version (always None in JSONB mode)
      - updated_at, created_at, source_type
    """
    if event_ids is None and relevant_log_events is None:
        raise ValueError("Either event_ids or relevant_log_events must be provided")

    def _apply_event_filter(query):
        if event_ids is not None:
            event_ids_selectable = (
                select(event_ids) if isinstance(event_ids, Subquery) else event_ids
            )
            return query.filter(LogEvent.id.in_(event_ids_selectable))
        query = query.join(relevant_log_events, relevant_log_events.c.id == LogEvent.id)
        if hasattr(relevant_log_events.c, "row_num"):
            query = query.order_by(relevant_log_events.c.row_num)
        return query

    def _field_type_subquery(field_name_expr, field_attr):
        base_conditions = [
            FieldType.project_id == LogEvent.project_id,
            FieldType.field_name == field_name_expr,
        ]
        if context_id is None:
            return (
                select(getattr(FieldType, field_attr))
                .where(*base_conditions, FieldType.context_id.is_(None))
                .scalar_subquery()
            )
        ctx_subq = (
            select(getattr(FieldType, field_attr))
            .where(*base_conditions, FieldType.context_id == context_id)
            .scalar_subquery()
        )
        global_subq = (
            select(getattr(FieldType, field_attr))
            .where(*base_conditions, FieldType.context_id.is_(None))
            .scalar_subquery()
        )
        return func.coalesce(ctx_subq, global_subq)

    if key:
        value_expr = LogEvent.data.op("->")(key)
        inferred_type = func.coalesce(
            _field_type_subquery(literal(key), "field_type"),
            literal("Any"),
        )
        field_category = _field_type_subquery(literal(key), "field_category")

        source_type = case(
            (field_category == "derived_entry", literal("derived")),
            else_=literal("base"),
        )

        query = session.query(
            LogEvent.id.label("id"),
            LogEvent.id.label("log_event_id"),
            literal(key).label("key"),
            value_expr.label("value"),
            inferred_type.label("inferred_type"),
            cast(None, Integer).label("param_version"),
            cast(None, Integer).label("context_version"),
            LogEvent.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            source_type.label("source_type"),
        ).select_from(LogEvent)
        query = _apply_event_filter(query)
        query = query.filter(LogEvent.data.has_key(key))
        return query.subquery(name="unified_logs")

    data_expr = func.coalesce(LogEvent.data, cast(literal("{}"), JSONB))
    data_each = lateral(
        func.jsonb_each(data_expr).table_valued("key", "value"),
    ).alias("data_each")

    inferred_type = func.coalesce(
        _field_type_subquery(data_each.c.key, "field_type"),
        literal("Any"),
    )
    field_category = _field_type_subquery(data_each.c.key, "field_category")

    source_type = case(
        (field_category == "derived_entry", literal("derived")),
        else_=literal("base"),
    )

    query = (
        session.query(
            LogEvent.id.label("id"),
            LogEvent.id.label("log_event_id"),
            data_each.c.key.label("key"),
            data_each.c.value.label("value"),
            inferred_type.label("inferred_type"),
            cast(None, Integer).label("param_version"),
            cast(None, Integer).label("context_version"),
            LogEvent.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            source_type.label("source_type"),
        )
        .select_from(LogEvent)
        .join(data_each, true())
    )

    query = _apply_event_filter(query)

    return query.subquery(name="unified_logs")


######################
# Formatting utils    #
######################


def _flatten_fields(
    log_fields: list,
):
    flattened = dict()
    for log_ids, fields in log_fields:
        log_ids = log_ids if isinstance(log_ids, list) else [log_ids]
        fields = fields if isinstance(fields, list) else [fields]
        for log_id in log_ids:
            if log_id not in flattened:
                flattened[log_id] = list()
            for field in fields:
                if field is not None and field not in flattened[log_id]:
                    flattened[log_id].append(field)
    return flattened


def is_image_field(field_name: str, field_types: dict) -> bool:
    """Check if a field is an image type."""
    return field_types.get(field_name) == "image"


def is_audio_field(field_name: str, field_types: dict) -> bool:
    """Check if a field is an audio type."""
    return field_types.get(field_name) == "audio"


def _format_flat_logs(rows, context_len, value_limit, field_order_map):
    """Helper function to format flat logs using raw query data"""
    formatted = {}

    for (
        row_key,
        row_value,
        row_inferred_type,
        row_param_version,
        row_context_version,
        row_source_type,
        row_created_at,
        row_event_id,
    ) in rows:

        if row_event_id not in formatted:
            formatted[row_event_id] = {
                "ts": row_created_at.isoformat() if row_created_at else None,
                "clipped_fields": [],
                "entries": {},
                "versions": {},
                "context_versions": {},
                "derived_entries": {},
            }

        is_derived = row_source_type == "derived"

        # Apply context_len slicing to the key to strip the context prefix
        key = row_key[context_len:] if context_len else row_key

        def _limit_value(value: any, inferred_type: str) -> tuple:
            """Limit the size of a value based on its type and the value_limit parameter.
            Returns a tuple of (limited_value, is_clipped)."""
            if value_limit is None:
                return value, False

            # Handle numeric values - return as is
            if inferred_type in ["int", "float", "bool"]:
                return value, False

            if inferred_type == "image" or inferred_type == "audio":
                return "", True

            if inferred_type in ["list", "dict", "tuple"]:
                str_value = str(value)
                if len(str_value) > value_limit:
                    return str_value[:value_limit] + "...", True
                return str_value, False

            # Handle string values
            if inferred_type == "str":
                if len(str(value)) > value_limit:
                    return str(value)[:value_limit] + "...", True
                return value, False

            # Default case - treat as string
            str_value = str(value)
            if len(str_value) > value_limit:
                return str_value[:value_limit] + "...", True
            return str_value, False

        # Apply value limiting and get clipped status
        limited_val, is_clipped = _limit_value(row_value, row_inferred_type)
        if is_clipped:
            formatted[row_event_id]["clipped_fields"].append(key)

        if is_derived:
            formatted[row_event_id]["derived_entries"][key] = limited_val
        else:
            if row_param_version is not None:
                # param-based version
                if key not in formatted[row_event_id]["versions"]:
                    formatted[row_event_id]["versions"][key] = {}
                formatted[row_event_id]["versions"][key][
                    row_param_version
                ] = limited_val
                formatted[row_event_id]["entries"][key] = str(row_param_version)

            elif row_context_version is not None:
                # context-based version
                if key not in formatted[row_event_id]["context_versions"]:
                    formatted[row_event_id]["context_versions"][key] = {}
                formatted[row_event_id]["context_versions"][key][
                    row_context_version
                ] = limited_val
                if key not in formatted[row_event_id]["entries"]:
                    formatted[row_event_id]["entries"][key] = limited_val

            else:
                # entries
                formatted[row_event_id]["entries"][key] = limited_val

    # Now build final JSON
    logs_out = []
    for event_id, data in formatted.items():
        # All fields go into entries now (no separate params)
        entries = dict(data["entries"])

        # derived_entries
        derived_entries = data["derived_entries"]

        # Sort all dictionaries according to field_type order
        sorted_entries = dict(
            sorted(
                entries.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        sorted_derived = dict(
            sorted(
                derived_entries.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        # sort keys which are strings by descending order
        sorted_context_versions = {
            field: dict(sorted(versions.items(), key=lambda x: x[0], reverse=True))
            for field, versions in data["context_versions"].items()
        }
        logs_out.append(
            {
                "id": event_id,
                "ts": data["ts"],
                "entries": sorted_entries,
                "derived_entries": sorted_derived,
                "versions": sorted_context_versions,
                "clipped_fields": data.get("clipped_fields", []),
            },
        )

    return logs_out, {}


def _format_logs(
    rows: list,
    field_types: dict,
    value_limit: Optional[int],
    column_context: Optional[str],
    field_order_map: dict,
    from_fields: Optional[str] = None,
    exclude_fields: Optional[str] = None,
) -> tuple:
    """
    Format JSONB query results into the API response format.

    Format JSONB query results into the API response format. Transforms (id, data, key_order, created_at) tuples
    into the standard log response structure.

    Args:
        rows: List of (id, data, key_order, created_at) tuples from JSONB query
        field_types: Dict from get_field_types(return_mutable=True) with metadata
                     Structure: {field_name: {"field_type": str, "field_category": str, ...}}
        value_limit: Optional int for string truncation
        column_context: Optional str for prefix filtering (e.g., "alpha/" or "entries/alpha/")
        field_order_map: Dict mapping field names to sort order
        from_fields: Optional ampersand-separated list of fields to include
        exclude_fields: Optional ampersand-separated list of fields to exclude

    Returns:
        Tuple of (logs_out, params_out) where:
        - logs_out: List of formatted log dicts with {id, ts, entries, params, derived_entries, clipped_fields}
        - params_out: Global dict of param versions {"param_name": {"0": value}}

    Note:
        Context versioning isn't supported, so response omits the 'versions' key.

        key_order is used to preserve nested dict key ordering since PostgreSQL JSONB
        alphabetizes keys. If key_order is None (legacy data), alphabetical order is used.
    """
    # Parse column_context to extract prefix
    context_prefix = ""

    if column_context is not None:
        split_context = column_context.split("/")
        # Remove legacy "params" and "entries" keywords from column context
        split_context = [s for s in split_context if s not in ("params", "entries")]

        # Build context prefix from remaining parts
        context_prefix = "/".join(split_context)
        # Ensure trailing "/" if non-empty for prefix matching
        if context_prefix and context_prefix[-1] != "/":
            context_prefix += "/"

    # Parse field filters
    allowed_fields_set = set(from_fields.split("&")) if from_fields else None
    excluded_fields_set = set(exclude_fields.split("&")) if exclude_fields else None

    def _limit_value(value: any, inferred_type: str) -> tuple:
        """Limit the size of a value based on its type and the value_limit parameter.
        Returns a tuple of (limited_value, is_clipped)."""
        if value_limit is None:
            return value, False

        # Handle None values
        if value is None:
            return None, False

        # Handle numeric values - return as is
        # Check both the stored field_type AND the actual Python type of the value
        # This handles edge cases where type metadata might be missing
        if inferred_type in ["int", "float", "bool"] or isinstance(
            value,
            (int, float, bool),
        ):
            return value, False

        if inferred_type in ["image", "audio"]:
            return "", True

        if inferred_type in ["list", "dict", "tuple"] or isinstance(
            value,
            (list, dict, tuple),
        ):
            str_value = str(value)
            if len(str_value) > value_limit:
                return str_value[:value_limit] + "...", True
            return str_value, False

        # Handle string values
        if inferred_type == "str" or isinstance(value, str):
            if len(str(value)) > value_limit:
                return str(value)[:value_limit] + "...", True
            return value, False

        # Default case - treat as string
        str_value = str(value)
        if len(str_value) > value_limit:
            return str_value[:value_limit] + "...", True
        return str_value, False

    # Process rows into formatted structure
    formatted = {}

    for row in rows:
        # Unpack row - handle both old format (id, data, created_at) and new format (id, data, key_order, created_at)
        if len(row) == 4:
            event_id, data, key_order, created_at = row
        else:
            # Legacy format without key_order
            event_id, data, created_at = row
            key_order = None

        if event_id not in formatted:
            formatted[event_id] = {
                "ts": created_at.isoformat() if created_at else None,
                "clipped_fields": [],
                "entries": {},
                "derived_entries": {},
            }

        # Handle None or empty data
        if not data:
            continue

        # Reorder nested dict keys using stored key_order to preserve original insertion order
        # This is needed because PostgreSQL JSONB alphabetizes keys
        data = reorder_nested_dict(data, key_order)

        for key, value in data.items():
            # Apply column_context prefix filter
            if context_prefix and not key.startswith(context_prefix):
                continue

            # Strip context_prefix from key if present
            display_key = key[len(context_prefix) :] if context_prefix else key

            # Apply from_fields / exclude_fields filters
            if allowed_fields_set and key not in allowed_fields_set:
                continue
            if excluded_fields_set and key in excluded_fields_set:
                continue

            # Get field metadata
            field_meta = field_types.get(key, {})
            field_type = field_meta.get("field_type", "str")
            field_category = field_meta.get("field_category", "entry")

            # Apply value limiting
            limited_val, is_clipped = _limit_value(value, field_type)
            if is_clipped:
                formatted[event_id]["clipped_fields"].append(display_key)

            # Categorize field based on field_category
            if field_category == "derived_entry":
                # Derived entries
                formatted[event_id]["derived_entries"][display_key] = limited_val
            else:
                # All other fields (entry and former param) go to entries
                formatted[event_id]["entries"][display_key] = limited_val

    # Build final JSON output with sorted fields
    logs_out = []
    for event_id, data in formatted.items():
        # Sort all dictionaries according to field_type order
        sorted_entries = dict(
            sorted(
                data["entries"].items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        sorted_derived = dict(
            sorted(
                data["derived_entries"].items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )

        # Skip logs with no data when field filters are applied
        # Logs without any matching fields don't appear
        if (allowed_fields_set or excluded_fields_set or context_prefix) and not (
            sorted_entries or sorted_derived
        ):
            continue

        logs_out.append(
            {
                "id": event_id,
                "ts": data["ts"],
                "entries": sorted_entries,
                "derived_entries": sorted_derived,
                "versions": {},  # JSONB mode doesn't support context versions, but include empty for API compatibility
                "clipped_fields": data.get("clipped_fields", []),
            },
        )
    return logs_out, {}


def _get_final_logs(session, filtered_logs_subq, paginated_ids_subq):
    """
    Return fully-hydrated rows from JSONB unified logs.

    The filtered_logs_subq already contains the value column, so we only
    need to join with paginated_ids_subq to preserve ordering.
    """
    final_logs_query = (
        session.query(
            filtered_logs_subq.c.id,
            filtered_logs_subq.c.log_event_id,
            filtered_logs_subq.c.key,
            filtered_logs_subq.c.value.label("value"),
            filtered_logs_subq.c.inferred_type,
            filtered_logs_subq.c.param_version,
            filtered_logs_subq.c.context_version,
            filtered_logs_subq.c.created_at,
            filtered_logs_subq.c.source_type,
        )
        .join(
            paginated_ids_subq,
            paginated_ids_subq.c.id == filtered_logs_subq.c.log_event_id,
        )
        .order_by(paginated_ids_subq.c.row_num, filtered_logs_subq.c.created_at)
    )

    # Capture SQL for test analysis (if enabled)
    try:
        from sqlalchemy import text

        from orchestra.tests.test_log.sql_capture import capture_sql, is_capture_enabled

        if is_capture_enabled():
            compiled_sql = final_logs_query.statement.compile(
                dialect=session.bind.dialect,
                compile_kwargs={"literal_binds": True},
            ).string
            explain_sql = (
                "EXPLAIN (ANALYZE, BUFFERS, TIMING, COSTS, VERBOSE, FORMAT JSON) "
                + compiled_sql
            )
            explain_result = session.execute(text(explain_sql))
            explain_output = explain_result.fetchone()[0]
            capture_sql(
                sql=compiled_sql,
                explain_analyze=explain_output,
            )
    except ImportError:
        pass
    except Exception:
        pass

    return final_logs_query.all()


#### JOIN LOG ####
def _build_log_subquery(
    args: Dict[str, Any],
    project_name: str,
    project_id: int,
    request_fastapi: Optional[Request],
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session,
    alias: str,
):
    """
    Build subquery selecting log_event_id and data column.

    Args:
        args: Dictionary containing filtering criteria
        project_name: Name of the project
        project_id: ID of the project
        request_fastapi: FastAPI request object
        project_dao: ProjectDAO instance
        field_type_dao: FieldTypeDAO instance
        context_dao: ContextDAO instance
        session: SQLAlchemy session
        alias: Alias name for the subquery

    Returns:
        Tuple of (subquery, field_names_dict)
    """
    # Import the necessary function from views.py to build subqueries
    from orchestra.web.api.log.views import _get_all_filtered_log_event_ids

    # Extract filtering criteria from args
    context = args.get("context")
    filter_expr = args.get("filter_expr")
    from_ids = args.get("from_ids")
    exclude_ids = args.get("exclude_ids")

    # Get filtered log event IDs as a subquery
    event_ids_subq, _ = _get_all_filtered_log_event_ids(
        request_fastapi=request_fastapi,
        project_name=project_name,
        context=context,
        filter_expr=filter_expr,
        from_ids=from_ids,
        exclude_ids=exclude_ids,
        project_dao=project_dao,
        context_dao=context_dao,
        field_type_dao=field_type_dao,
        session=session,
        as_subquery=True,
    )

    # Get context ID for field type lookup
    context_id = None
    if context:
        context_id = context_dao.get_or_create(
            project_id,
            name=context,
        )

    # Build simple query selecting log_event_id and data column
    base_query = session.query(
        LogEvent.id.label("log_event_id"),
        LogEvent.data.label("data"),
    )

    # Get field names from FieldTypeDAO for validation and column selection
    field_names_dict = {}
    try:
        field_names_dict = field_type_dao.get_field_types(
            project_id=project_id,
            context_id=context_id,
        )
    except Exception as e:
        raise ValueError(f"Error getting field types: {str(e)}")

    # Apply the filter to get only the log events we want
    final_query = base_query.filter(
        LogEvent.id.in_(select(event_ids_subq)),
    ).order_by(LogEvent.id.asc())

    # Return as a subquery with the specified alias
    return final_query.subquery(alias), field_names_dict


def _construct_join_query(
    subq_a,
    subq_b,
    join_expr: str,
    mode: str,
    columns: Optional[Union[Dict[str, str], List[str]]] = None,
    fields_a: Optional[Dict[str, Any]] = None,
    fields_b: Optional[Dict[str, Any]] = None,
    include_log_ids: bool = False,
    session=None,
):
    """
    JSONB version: Construct join using A.data || B.data merge.

    Uses PostgreSQL's native JSONB merge operator (||) for O(1) field merging.
    Right side wins on key conflicts per PostgreSQL semantics.

    Args:
        subq_a: First subquery (aliased as 'A')
        subq_b: Second subquery (aliased as 'B')
        join_expr: SQL expression for the join condition
        mode: Type of join ('inner', 'left', 'right', or 'outer')
        columns: Optional dictionary mapping source columns to new column names
                 or list of source columns to include
        fields_a: Field types dict for subquery A
        fields_b: Field types dict for subquery B
        include_log_ids: Whether to include log_event_id columns
        session: SQLAlchemy session

    Returns:
        SQLAlchemy select statement representing the join with merged JSONB
    """
    # Import the necessary functions from python2SQL module
    from orchestra.web.api.log.python2SQL.core import build_sql_query
    from orchestra.web.api.log.python2SQL.parsers import (
        str_filter_exp_to_dict_using_ast,
    )

    try:
        # 1. Preprocess the join expression to replace A. and B. prefixes with placeholders
        processed_join_expr = re.sub(r"\bA\.(\w+)", r"__table_A_\1", join_expr)
        processed_join_expr = re.sub(
            r"\bB\.(\w+)",
            r"__table_B_\1",
            processed_join_expr,
        )

        # 2. Build the local_scope dictionary mapping placeholders to JSONB extractions
        local_scope = {
            "subq_a": subq_a,
            "subq_b": subq_b,
            "__comparison_context__": "join",
        }

        # Map field placeholders to JSONB -> extractions (preserves JSONB type for arrays/objects)
        # Using -> instead of ->> is important for functions like mean() that need array access
        # Pass the actual field type so aggregation functions can handle lists correctly
        if fields_a:
            for field_name, field_type in fields_a.items():
                # Use JSONB -> operator for JSONB extraction (preserves type)
                local_scope[f"__table_A_{field_name}"] = (
                    subq_a.c.data.op("->")(field_name),
                    field_type,  # Pass actual field type for proper aggregation handling
                )
        if fields_b:
            for field_name, field_type in fields_b.items():
                local_scope[f"__table_B_{field_name}"] = (
                    subq_b.c.data.op("->")(field_name),
                    field_type,  # Pass actual field type for proper aggregation handling
                )

        # 3. Parse the processed join expression into a filter dictionary
        filter_dict = str_filter_exp_to_dict_using_ast(processed_join_expr)

        # 4. Build the SQL query using the filter dictionary with the local_scope
        join_condition = build_sql_query(
            filter_dict,
            LogEvent,
            session=session,
            log_event_ids=select(subq_a.c.log_event_id).subquery("event_ids"),
            is_derived=False,
            local_scope=local_scope,
        )
    except Exception as e:
        raise ValueError(f"Error processing join expression: {e}")

    select_columns = []

    # If include_log_ids is True, always include log_event_id from both sources
    if include_log_ids:
        select_columns.append(subq_a.c.log_event_id.label("log_event_id_a"))
        select_columns.append(subq_b.c.log_event_id.label("log_event_id_b"))

    if columns:
        # Column selection mode: extract specific keys from JSONB and build merged_data
        if isinstance(columns, dict):
            column_specs = list(columns.items())
        elif isinstance(columns, list):
            column_specs = [(col, col.split(".", 1)[1]) for col in columns]
        else:
            raise ValueError("columns must be either a dictionary or a list")

        # Build a list of (label, jsonb_value) pairs to construct merged_data
        jsonb_build_args = []
        for source_col, label in column_specs:
            if "." not in source_col:
                raise ValueError(
                    f"Column '{source_col}' must be prefixed with table alias 'A.' or 'B.'",
                )

            table_alias, actual_col = source_col.split(".", 1)
            table_alias = table_alias.upper()

            if table_alias == "A":
                subq = subq_a
                fields = fields_a
                source_name = "source A"
            elif table_alias == "B":
                subq = subq_b
                fields = fields_b
                source_name = "source B"
            else:
                raise ValueError(
                    f"Invalid table alias '{table_alias}' in column '{source_col}'",
                )

            # Validate that the column exists in the source context
            if fields and actual_col not in fields:
                raise ValueError(f"Column '{actual_col}' not found in {source_name}")

            # Skip embedding (vector) columns - they are stored in the Embedding table,
            # not in LogEvent.data. They will be copied separately via the Embedding
            # table copy logic in _create_logs_from_joined_rows.
            field_type = fields.get(actual_col) if fields else None
            if field_type == "vector":
                continue

            # Extract value from JSONB using -> operator (preserves type as JSONB)
            jsonb_value = subq.c.data.op("->")(actual_col)
            jsonb_build_args.extend([literal(label), jsonb_value])

        # Build merged_data as a JSONB object from selected columns
        # Use jsonb_build_object to construct the JSONB from key-value pairs
        # Guard: if all requested columns are vectors (stored in Embedding table),
        # jsonb_build_args will be empty. In that case, use an empty JSONB object.
        if jsonb_build_args:
            merged_data = func.jsonb_build_object(*jsonb_build_args).label(
                "merged_data",
            )
        else:
            # All columns are vectors (embeddings) - produce empty JSONB object
            merged_data = cast(literal("{}"), JSONB).label("merged_data")
        select_columns.append(merged_data)
    else:
        # Full merge mode: use A.data || B.data to merge all fields
        # COALESCE handles NULL data in outer joins
        merged_data = (
            func.coalesce(
                subq_a.c.data,
                cast(literal("{}"), JSONB),
            )
            .op("||")(
                func.coalesce(subq_b.c.data, cast(literal("{}"), JSONB)),
            )
            .label("merged_data")
        )
        select_columns.append(merged_data)

    # Build the join query based on the mode
    if mode == "inner":
        joined_query = select(*select_columns).select_from(
            subq_a.join(subq_b, join_condition),
        )
    elif mode == "left":
        joined_query = select(*select_columns).select_from(
            subq_a.outerjoin(subq_b, join_condition),
        )
    elif mode == "right":
        joined_query = select(*select_columns).select_from(
            subq_b.outerjoin(subq_a, join_condition),
        )
    elif mode == "outer":
        joined_query = select(*select_columns).select_from(
            subq_b.outerjoin(subq_a, join_condition, full=True),
        )

    return joined_query


def _create_logs_from_joined_rows(
    result_rows,
    project_id: int,
    context_id: int,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session,
    source_contexts: Optional[Dict[str, int]] = None,
    columns: Optional[Union[Dict[str, str], List[str]]] = None,
) -> List[int]:
    """
    Insert merged JSONB directly into LogEvent.data.

    Args:
        result_rows: Result rows from the join query (contain merged_data JSONB)
        project_id: ID of the project
        context_id: ID of the context
        field_type_dao: FieldTypeDAO instance for field type operations
        context_dao: ContextDAO instance for context operations
        session: SQLAlchemy session
        source_contexts: Optional mapping of source aliases to context IDs
        columns: Optional column mapping from source to target names

    Returns:
        List of IDs of the newly created log events
    """
    new_log_ids = []
    now = datetime.now(timezone.utc)

    # Get the context object
    context_obj = session.get(Context, context_id)

    # Prepare collections for bulk operations
    new_field_types = []
    log_events = []
    log_event_contexts = []
    embeddings_to_create = []

    # Track new field types to avoid duplicates
    new_field_types_seen: set[tuple[int, str]] = set()

    # Build caches for field type lookups
    # Include both merged_data keys AND column target names (for embedding fields
    # that are not in merged_data but still need field type copying)
    all_field_names: set[str] = set()
    for row in result_rows:
        merged_data = getattr(row, "merged_data", None)
        if merged_data and isinstance(merged_data, dict):
            all_field_names.update(merged_data.keys())

    # Add column target names (handles embedding fields excluded from merged_data)
    if columns:
        if isinstance(columns, dict):
            for source_col, target_name in columns.items():
                all_field_names.add(target_name)
                # Also add source column key for lookup
                if "." in source_col:
                    _, source_key = source_col.split(".", 1)
                    all_field_names.add(source_key)
        elif isinstance(columns, list):
            for source_col in columns:
                if "." in source_col:
                    _, source_key = source_col.split(".", 1)
                    all_field_names.add(source_key)

    # Cache source context field types
    source_ft_cache: dict[tuple[int | None, str], FieldType | None] = {}
    if source_contexts:
        for src_ctx in source_contexts.values():
            if src_ctx is not None:
                fts = (
                    session.query(FieldType)
                    .filter(
                        FieldType.project_id == project_id,
                        FieldType.context_id == src_ctx,
                        FieldType.field_name.in_(list(all_field_names)),
                    )
                    .all()
                )
                for ft in fts:
                    source_ft_cache[(src_ctx, ft.field_name)] = ft

    # Global field types (context_id = None)
    fts_global = (
        session.query(FieldType)
        .filter(
            FieldType.project_id == project_id,
            FieldType.context_id.is_(None),
            FieldType.field_name.in_(list(all_field_names)),
        )
        .all()
    )
    for ft in fts_global:
        source_ft_cache[(None, ft.field_name)] = ft

    # Cache target context field types
    target_ft_cache: dict[str, FieldType] = {}
    fts_target = (
        session.query(FieldType)
        .filter(
            FieldType.project_id == project_id,
            FieldType.context_id == context_id,
            FieldType.field_name.in_(list(all_field_names)),
        )
        .all()
    )
    for ft in fts_target:
        target_ft_cache[ft.field_name] = ft

    # Process each row
    for row in result_rows:
        # Extract merged JSONB data
        merged_data = getattr(row, "merged_data", None)
        if merged_data is None:
            merged_data = {}

        # Create a new LogEvent with JSONB data
        log_event = LogEvent(
            project_id=project_id,
            data=merged_data,  # Direct JSONB assignment
            created_at=now,
            updated_at=now,
        )
        log_events.append(log_event)
        session.add(log_event)

    # Flush to get IDs
    session.flush()

    # Create LogEventContext associations and handle field types
    for i, log_event in enumerate(log_events):
        row = result_rows[i]
        merged_data = getattr(row, "merged_data", None) or {}

        # Create LogEventContext association
        log_event_contexts.append(
            LogEventContext(
                log_event_id=log_event.id,
                context_id=context_id,
            ),
        )

        # Build column source mapping for looking up original field types
        # This is needed to handle embedding columns that are not in merged_data
        column_source_map_for_ft: Dict[str, tuple] = {}
        if columns:
            if isinstance(columns, dict):
                for source_col, target_name in columns.items():
                    if "." in source_col:
                        table_alias, source_key = source_col.split(".", 1)
                        column_source_map_for_ft[target_name] = (
                            table_alias.upper(),
                            source_key,
                        )
            elif isinstance(columns, list):
                for source_col in columns:
                    if "." in source_col:
                        table_alias, source_key = source_col.split(".", 1)
                        column_source_map_for_ft[source_key] = (
                            table_alias.upper(),
                            source_key,
                        )

        # Process field types for fields in merged data AND for embedding columns
        # that are specified in columns but not in merged_data (since embeddings
        # are stored in the Embedding table, not in JSONB)
        cols_to_process = set(merged_data.keys()) | set(column_source_map_for_ft.keys())

        for col in cols_to_process:
            val = merged_data.get(col)  # May be None for embedding columns

            # Look up the original field type using pre-fetched caches
            original_field_type = None

            # First, try to find using the source context mapping
            if col in column_source_map_for_ft:
                source_table, source_key = column_source_map_for_ft[col]
                # Determine which source context to look in
                if source_contexts:
                    src_ctx = source_contexts.get(source_table)
                    if src_ctx:
                        original_field_type = source_ft_cache.get(
                            (src_ctx, source_key),
                        )

            # Fallback to checking all source contexts
            if original_field_type is None and source_contexts:
                for src_ctx in source_contexts.values():
                    ft = source_ft_cache.get((src_ctx, col))
                    if ft is not None:
                        original_field_type = ft
                        break

            if original_field_type is None:
                original_field_type = source_ft_cache.get((None, col))

            # Check if field already exists in target context
            existing_field_type = target_ft_cache.get(col)

            if existing_field_type:
                # Validate type consistency (skip for embedding columns with no value)
                if val is not None:
                    from orchestra.web.api.log.utils.type_utils import (
                        is_untyped_field,
                        types_match,
                    )

                    entered_type = LogEventDAO.infer_type(col, val)

                    if not is_untyped_field(existing_field_type.field_type):
                        if not types_match(
                            existing_field_type.field_type,
                            entered_type,
                        ):
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"Type mismatch for field '{col}' in joined result: "
                                    f"expected {existing_field_type.field_type}, got {entered_type}"
                                ),
                            )
            elif original_field_type:
                # Copy the original field type to the new context
                key_ft = (context_id, col)
                if key_ft not in new_field_types_seen:
                    new_field_types_seen.add(key_ft)
                    new_field_types.append(
                        {
                            "project_id": project_id,
                            "field_name": col,
                            "value": val,
                            "mutable": original_field_type.mutable,
                            "unique": original_field_type.unique,
                            "field_category": original_field_type.field_category,
                            "enum_values": original_field_type.enum_values,
                            "enum_restrict": original_field_type.enum_restrict,
                            "description": original_field_type.description,
                            "context_id": context_id,
                            # Use the original field type for embeddings
                            "field_type": original_field_type.field_type,
                        },
                    )
                    # Add to cache to avoid duplicates in same batch
                    target_ft_cache[col] = original_field_type
            else:
                # No original field type found, infer from value (skip if no value)
                if val is not None:
                    mutable = context_obj and context_obj.is_versioned

                    key_ft = (context_id, col)
                    if key_ft not in new_field_types_seen:
                        new_field_types_seen.add(key_ft)
                        new_field_types.append(
                            {
                                "project_id": project_id,
                                "field_name": col,
                                "value": val,
                                "mutable": mutable,
                                "unique": False,
                                "field_category": "entry",
                                "context_id": context_id,
                            },
                        )

        new_log_ids.append(log_event.id)

    # Preserve embeddings from source log events
    source_log_event_ids = set()
    for row in result_rows:
        log_event_id_a = getattr(row, "log_event_id_a", None)
        log_event_id_b = getattr(row, "log_event_id_b", None)
        if log_event_id_a:
            source_log_event_ids.add(log_event_id_a)
        if log_event_id_b:
            source_log_event_ids.add(log_event_id_b)

    if source_log_event_ids:
        # Query all embeddings from source log events
        # Exclude soft-deleted embeddings from copying operations
        source_embeddings = (
            session.query(Embedding)
            .filter(
                Embedding.ref_id.in_(source_log_event_ids),
                Embedding.is_deleted
                == False,  # noqa: E712 - SQLAlchemy requires == for SQL generation
            )
            .all()
        )

        # Build lookup map: (ref_id, key) -> embedding
        embedding_lookup = {}
        for emb in source_embeddings:
            embedding_lookup[(emb.ref_id, emb.key)] = emb

        # Build a mapping from target column name to (source_table, source_key)
        # This helps us look up embeddings by the original source key
        column_source_map: Dict[str, tuple] = {}
        if columns:
            if isinstance(columns, dict):
                for source_col, target_name in columns.items():
                    if "." in source_col:
                        table_alias, source_key = source_col.split(".", 1)
                        column_source_map[target_name] = (
                            table_alias.upper(),
                            source_key,
                        )
            elif isinstance(columns, list):
                for source_col in columns:
                    if "." in source_col:
                        table_alias, source_key = source_col.split(".", 1)
                        # For list format, target name is the source key
                        column_source_map[source_key] = (
                            table_alias.upper(),
                            source_key,
                        )

        # Copy embeddings to new log events
        for i, (log_event, row) in enumerate(zip(log_events, result_rows)):
            log_event_id_a = getattr(row, "log_event_id_a", None)
            log_event_id_b = getattr(row, "log_event_id_b", None)
            merged_data = getattr(row, "merged_data", None) or {}

            # Build a combined set of columns to check for embeddings:
            # 1. All keys in merged_data (regular fields that might have embeddings)
            # 2. All target column names from column_source_map (includes embedding
            #    fields that were skipped from merged_data but should be copied)
            cols_to_check = set(merged_data.keys()) | set(column_source_map.keys())

            # Check each field for embeddings
            for col in cols_to_check:
                # Determine which source to look in and what key to use
                source_table = None
                source_key = col  # Default to same key name

                if col in column_source_map:
                    source_table, source_key = column_source_map[col]

                # Determine which source log event ID to use
                if source_table == "A":
                    source_ids = [log_event_id_a] if log_event_id_a else []
                elif source_table == "B":
                    source_ids = [log_event_id_b] if log_event_id_b else []
                else:
                    # No specific source, try both
                    source_ids = [s for s in [log_event_id_a, log_event_id_b] if s]

                for source_id in source_ids:
                    embedding = embedding_lookup.get((source_id, source_key))
                    if embedding:
                        embeddings_to_create.append(
                            {
                                "log_event_id": log_event.id,
                                "key": col,  # Use target column name for the new embedding
                                "vector": embedding.vector,
                                "model": embedding.model,
                            },
                        )
                        break  # Found embedding for this key

    # Bulk create new field types if any
    try:
        if new_field_types:
            field_type_dao.bulk_create_field_types(new_field_types)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Bulk insert LogEventContext associations
    session.bulk_save_objects(log_event_contexts)

    # Create Embedding entries
    if embeddings_to_create:
        for emb_data in embeddings_to_create:
            embedding = Embedding(
                ref_id=emb_data["log_event_id"],
                key=emb_data["key"],
                model=emb_data["model"],
                vector=emb_data["vector"],
            )
            session.add(embedding)

    # Flush to ensure embeddings are saved
    session.flush()

    return new_log_ids


def _join_logs(
    project_id: int,
    project_name: str,
    pair_of_args: List[Dict[str, Any]],
    join_expr: str,
    mode: str,
    context_id: int,
    columns: Optional[Union[Dict[str, str], List[str]]] = None,
    copy: bool = True,
    request_fastapi: Optional[Request] = None,
    project_dao: ProjectDAO = None,
    field_type_dao: FieldTypeDAO = None,
    context_dao: ContextDAO = None,
    session=None,
) -> List[int]:
    """
    Join logs from two different queries and create new log entries with the joined data.

    This method performs a SQL-based join between two sets of logs, using SQLAlchemy to
    construct and execute the join query directly in the database. It avoids materializing
    large result sets in Python memory by delegating the join operation to the database.

    Only `copy=True` is supported. JSONB stores data inline in LogEvent.data,
    so all joins create independent copies. Passing `copy=False` will raise a ValueError.

    Args:
        project_id: ID of the project containing the logs
        project_name: Name of the project
        pair_of_args: List of two dictionaries containing filtering criteria for logs to join
        join_expr: SQL expression for the join condition using aliases A and B
                   (e.g., 'A.user_id = B.user_id')
        mode: Type of join to perform ('inner', 'left', 'right', or 'outer')
        context_id: ID of the context where joined logs will be stored
        columns: Optional column specification. Dictionary mapping source columns to new column names:
                 {'A.column_name': 'new_name', 'B.column_name': 'other_name'}
        copy: Must be True. Creates copies of the logs with independent data.
        request_fastapi: FastAPI request object for accessing user state
        project_dao: ProjectDAO instance for project operations
        field_type_dao: FieldTypeDAO instance for field type operations
        context_dao: ContextDAO instance for context operations
        session: SQLAlchemy session

    Returns:
        List of IDs of the newly created log entries

    Raises:
        ValueError: If the join parameters are invalid, if copy=False is used in JSONB mode,
                    or if any other error occurs
    """
    # JSONB mode - all log data is stored in LogEvent.data
    if not copy:
        raise ValueError(
            "Pass-by-reference (copy=False) is not supported. "
            "JSONB stores data inline, so all joins create independent copies. "
            "Please set copy=True.",
        )
    return _join_logs_internal(
        project_id=project_id,
        project_name=project_name,
        pair_of_args=pair_of_args,
        join_expr=join_expr,
        mode=mode,
        context_id=context_id,
        columns=columns,
        request_fastapi=request_fastapi,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
    )


def _join_logs_internal(
    project_id: int,
    project_name: str,
    pair_of_args: List[Dict[str, Any]],
    join_expr: str,
    mode: str,
    context_id: int,
    columns: Optional[Union[Dict[str, str], List[str]]] = None,
    request_fastapi: Optional[Request] = None,
    project_dao: ProjectDAO = None,
    field_type_dao: FieldTypeDAO = None,
    context_dao: ContextDAO = None,
    session=None,
) -> List[int]:
    """
    JSONB version: Join logs using A.data || B.data merge.

    This implementation uses PostgreSQL's native JSONB merge operator (||) for
    efficient field merging.

    Args:
        project_id: ID of the project containing the logs
        project_name: Name of the project
        pair_of_args: List of two dictionaries containing filtering criteria for logs to join
        join_expr: SQL expression for the join condition using aliases A and B
        mode: Type of join to perform ('inner', 'left', 'right', or 'outer')
        context_id: ID of the context where joined logs will be stored
        columns: Optional column specification (Dict for aliasing, List for selection)
        request_fastapi: FastAPI request object for accessing user state
        project_dao: ProjectDAO instance for project operations
        field_type_dao: FieldTypeDAO instance for field type operations
        context_dao: ContextDAO instance for context operations
        session: SQLAlchemy session

    Returns:
        List of IDs of the newly created log entries

    Raises:
        ValueError: If the join parameters are invalid or if any other error occurs
    """
    try:
        # Extract and validate contexts
        context_a = pair_of_args[0].get("context")
        context_b = pair_of_args[1].get("context")
        if not context_a or not context_b:
            raise ValueError(
                f"Contexts for both queries must be provided in the pair of args. "
                f"Got: {context_a} and {context_b}",
            )

        # Preprocess filter expressions to remove context prefixes
        filter_expr_a = pair_of_args[0].get("filter_expr")
        filter_expr_b = pair_of_args[1].get("filter_expr")
        if filter_expr_a:
            pair_of_args[0]["filter_expr"] = filter_expr_a.replace(context_a + ".", "")
        if filter_expr_b:
            pair_of_args[1]["filter_expr"] = filter_expr_b.replace(context_b + ".", "")

        # Replace context names with A/B aliases in join expression
        join_expr = join_expr.replace(context_a, "A").replace(context_b, "B")

        # Process columns if provided
        if columns is not None:
            if isinstance(columns, dict):
                new_columns = {}
                for source_col, new_alias in columns.items():
                    processed_source_col = source_col.replace(context_a, "A").replace(
                        context_b,
                        "B",
                    )
                    new_columns[processed_source_col] = new_alias
                columns = new_columns
            elif isinstance(columns, list):
                new_columns = []
                for source_col in columns:
                    processed_source_col = source_col.replace(context_a, "A").replace(
                        context_b,
                        "B",
                    )
                    new_columns.append(processed_source_col)
                columns = new_columns
            else:
                raise ValueError(
                    "columns must be either a dictionary (for aliasing) "
                    "or a list (for column selection)",
                )

        # Build JSONB subqueries for both contexts
        subq_a, fields_a = _build_log_subquery(
            args=pair_of_args[0],
            project_name=project_name,
            project_id=project_id,
            request_fastapi=request_fastapi,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            alias="A",
        )
        subq_b, fields_b = _build_log_subquery(
            args=pair_of_args[1],
            project_name=project_name,
            project_id=project_id,
            request_fastapi=request_fastapi,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            alias="B",
        )

        # Construct the JSONB join query
        joined_query = _construct_join_query(
            subq_a=subq_a,
            subq_b=subq_b,
            join_expr=join_expr,
            mode=mode,
            columns=columns,
            fields_a=fields_a,
            fields_b=fields_b,
            include_log_ids=True,  # Always include log IDs for embedding lookups
            session=session,
        )

        # Execute the join query
        result_rows = session.execute(joined_query).fetchall()

        # If no results, return empty list
        if not result_rows:
            return []

        # Get source context IDs for field type lookups
        source_contexts = {}
        context_a_id = context_dao.get_or_create(project_id, name=context_a)
        context_b_id = context_dao.get_or_create(project_id, name=context_b)
        source_contexts["A"] = context_a_id
        source_contexts["B"] = context_b_id

        # Create new log entries from the joined results using JSONB merge
        new_log_ids = _create_logs_from_joined_rows(
            result_rows=result_rows,
            project_id=project_id,
            context_id=context_id,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            source_contexts=source_contexts,
            columns=columns,
        )

        # Commit the transaction
        session.commit()
        return new_log_ids

    except Exception as e:
        raise ValueError(f"Failed to join logs (JSONB): {traceback.format_exc()}")
