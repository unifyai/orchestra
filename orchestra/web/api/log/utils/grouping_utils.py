import json
from enum import Enum
from json import JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import (
    Float,
    Integer,
    Text,
    and_,
    asc,
    cast,
    desc,
    exists,
    func,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import CTE, Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Log,
    LogEvent,
    LogEventContext,
    LogEventLog,
)

from ..python2SQL import build_sql_query, str_filter_exp_to_dict
from .logging_utils import (
    _build_sort_criteria,
    _build_unified_logs_limited,
    _build_unified_logs_subquery,
    _format_flat_logs,
    _get_final_logs,
    is_image_field,
)
from .metric_utils import AggregationMetric, _get_reduction_expr

__all__ = [
    "_get_distinct_group_values",
    "_get_distinct_group_values",
    "_get_log_event_ids_for_group_value",
    "_get_log_event_ids_for_group_value",
    "_get_params_for_log_events",
    "_fetch_logs_for_event_ids",
    "_build_grouped_data",
    "_build_grouped_data",
    "_get_all_filtered_log_event_ids",
    "_handle_group_depth_level",
    "_handle_group_depth_level",
    "parse_group_key",
    "apply_group_threshold",
    "_fetch_leaf_logs",
    # GROUPING SETS optimization functions
    "_build_grouping_sets_query",
    "_reconstruct_nested_structure",
    "_can_use_grouping_sets",
    "_get_grouping_level",
    "_build_grouped_data_with_grouping_sets",
]
#####################
# GroupBy Utils     #
#####################


# Sorting configuration modes
class SortType(str, Enum):
    WITHIN_GROUPS = "within_groups"
    SORT_GROUPS = "sort_groups"


class SortDirection(str, Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class SortConfig(BaseModel):
    field: str = Field(..., description="The field to sort by")
    direction: SortDirection = Field(..., description="Sort direction")
    sort_type: SortType = Field(
        default=SortType.SORT_GROUPS,
        description="Whether to sort within groups or sort groups themselves",
    )
    metric: Optional[AggregationMetric] = Field(
        None,
        description="Required when sort_type is sort_groups. The metric to use for group-level sorting.",
    )


GROUP_THRESHOLD = 100


#####################
# JSONB Helpers     #
#####################


def _extract_field(
    field_key: str,
    field_types: Dict[str, str],
    cast_type: Optional[str] = None,
) -> Any:
    """
    Build SQLAlchemy expression to extract and optionally cast a JSONB field.

    Args:
        field_key: The key to extract from LogEvent.data
        field_types: Field type mapping from FieldTypeDAO
        cast_type: Optional explicit cast type (float, int, text)

    Returns:
        SQLAlchemy ColumnElement for the extracted field

    Example:
        _extract_field('score', field_types)
        -> cast(LogEvent.data.op('->>')('score'), Float)
    """
    expr = LogEvent.data.op("->>")((field_key))

    if cast_type:
        type_map = {"float": Float, "int": Integer, "text": Text}
        return cast(expr, type_map.get(cast_type, Text))

    field_type = field_types.get(field_key, "str")
    if field_type in ("float", "int"):
        return cast(expr, Float if field_type == "float" else Integer)
    return expr


def _build_containment_filter(
    field_key: str,
    field_value: Any,
) -> Any:
    """
    Build JSONB containment filter for efficient group membership checks.

    Uses GIN index via @> operator for O(1) lookups.

    Args:
        field_key: The key to filter on
        field_value: The value to match

    Returns:
        SQLAlchemy BinaryExpression for data @> '{"key": "value"}'

    Example:
        _build_containment_filter('status', 'fail')
        -> LogEvent.data.op('@>')(cast('{"status": "fail"}', JSONB))
    """
    from sqlalchemy import literal

    containment_obj = json.dumps({field_key: field_value})
    return LogEvent.data.op("@>")(func.cast(literal(containment_obj), JSONB))


def _get_distinct_group_values(
    log_event_ids: List[int],
    group_key: str,
    session,
    field_types: Dict[str, str],
    sort_direction: Optional[str] = None,
) -> List[Any]:
    """
    Get distinct values for a group key using direct data column extraction.

    Args:
        log_event_ids: List of log event IDs to filter
        group_key: The field key to group by (e.g., 'score', 'model')
        session: Database session
        field_types: Field type mapping from FieldTypeDAO
        sort_direction: Optional 'ascending' or 'descending'

    Returns:
        List of distinct values for the group key
    """
    # Parse group key (remove 'entries/' or 'params/' prefix if present)
    prefix, raw_key = parse_group_key(group_key)

    # Parameter versioning not supported in current mode
    if prefix == "params":
        raise HTTPException(
            status_code=400,
            detail="Parameter versioning is not supported in JSONB mode. "
            "Use entries/ prefix or omit prefix entirely.",
        )

    # Build base query: SELECT DISTINCT data->>'raw_key' FROM log_event
    query = (
        session.query(
            func.distinct(LogEvent.data.op("->>")(raw_key)).label("value"),
        )
        .filter(LogEvent.id.in_(select(log_event_ids)))
        .filter(LogEvent.data.op("?")(raw_key))  # Key exists check
    )

    # Apply sorting - for DISTINCT queries, ORDER BY must reference selected columns
    field_type = field_types.get(raw_key, "str")
    if field_type in ("float", "int"):
        sort_expr = cast(LogEvent.data.op("->>")(raw_key), Float)
    else:
        sort_expr = LogEvent.data.op("->>")(raw_key)

    if sort_direction == "ascending":
        query = query.order_by(asc(sort_expr).nulls_last())
    elif sort_direction == "descending":
        query = query.order_by(desc(sort_expr).nulls_first())
    else:
        # Default: order by value for deterministic results (required for DISTINCT)
        query = query.order_by(asc(sort_expr).nulls_last())

    # Capture SQL for test analysis (if enabled)
    try:
        from sqlalchemy import text

        from orchestra.tests.test_log.sql_capture import (
            capture_sql,
            is_capture_enabled,
            set_test_context,
        )

        if is_capture_enabled():
            pass

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
            # Set context and capture
            set_test_context(
                test_name="distinct_group_values",
                filter_expr=f"distinct_groups({group_key})",
                mode=mode,
            )
            capture_sql(
                sql=compiled_sql,
                explain_analyze=explain_output,
                filter_expr_override=f"distinct_groups({group_key})",
            )
    except ImportError:
        pass  # sql_capture module not available (production environment)
    except Exception:
        pass  # Silently ignore capture errors

    return [row[0] for row in query.all()]


def _get_log_event_ids_for_group_value(
    log_event_ids: List[int],
    group_key: str,
    group_value: Any,
    session,
    field_types: Dict[str, str],
) -> List[int]:
    """
    Get log event IDs matching a specific group value.

    Args:
        log_event_ids: List of log event IDs to filter
        group_key: The field key to filter on
        group_value: The value to match
        session: Database session
        field_types: Field type mapping

    Returns:
        List of matching log event IDs
    """
    # Parse group key
    prefix, raw_key = parse_group_key(group_key)

    # Reject param versioning
    if prefix == "params":
        raise HTTPException(
            status_code=400,
            detail="Parameter versioning is not supported in JSONB mode. "
            "Use entries/ prefix or omit prefix.",
        )

    # Use text extraction for type-agnostic comparison
    # This matches how _get_distinct_group_values extracts values via ->>
    query = (
        session.query(LogEvent.id)
        .filter(LogEvent.id.in_(select(log_event_ids)))
        .filter(LogEvent.data.op("->>")(raw_key) == group_value)
    )

    return [row[0] for row in query.all()]


def _get_params_for_log_events(
    log_event_ids: Subquery,
    session,
) -> Dict[str, Dict[int, Any]]:
    """Get all parameter versions used across the log events."""
    query = (
        session.query(Log)
        .join(LogEventLog, LogEventLog.log_id == Log.id)
        .filter(LogEventLog.log_event_id.in_(select(log_event_ids)))
        .filter(Log.param_version.isnot(None))
    )

    params = {}
    for log in query.all():
        if log.key not in params:
            params[log.key] = {}
        params[log.key][log.param_version] = log.value

    return params


def apply_group_threshold(
    logs_out: List[Dict[str, Any]],
    group_threshold: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Given a list of logs (each a dict with 'entries'), find all (field, value) combos
    that appear in >= group_threshold logs, remove them from 'entries',
    and place them in a top-level 'grouped_entries' plus per-log 'shared_entries'.

    Returns:
      (updated_logs_out, grouped_entries_dict)
    """
    # Early return if group_threshold is None or invalid
    if group_threshold is None or group_threshold <= 0:
        return logs_out, {}

    # Track frequency of each field value across logs
    field_values = {}  # field -> value -> set(log_ids)
    for log in logs_out:
        for field, value in log["entries"].items():
            if field not in field_values:
                field_values[field] = {}
            value_str = json.dumps(value)
            if value_str not in field_values[field]:
                field_values[field][value_str] = set()
            field_values[field][value_str].add(log["id"])

    # Build grouped_entries dict for fields that meet the threshold
    grouped_entries = {}  # field -> value_dict
    fields_to_group = set()  # fields that have any values meeting threshold

    for field, values in field_values.items():
        # For group_threshold=1, we always group
        # For group_threshold>1, we only group if any value appears >= threshold times
        if group_threshold == 1 or any(
            len(log_ids) >= group_threshold for log_ids in values.values()
        ):

            # Add this field to grouped_entries with all its distinct values
            grouped_entries[field] = {}
            fields_to_group.add(field)

            # Map each log_id to its value for this field
            log_id_to_value = {}
            for value_str, log_ids in values.items():
                value = json.loads(value_str)
                for log_id in log_ids:
                    log_id_to_value[log_id] = value

            # Add all distinct values to grouped_entries
            for value in log_id_to_value.values():
                if value not in grouped_entries[field].values():
                    # Find next available index
                    next_idx = len(grouped_entries[field])
                    grouped_entries[field][next_idx] = value

    # Update each log to use shared_entries
    for log in logs_out:
        shared_entries = {}

        # For each field being grouped
        for field in fields_to_group:
            if field in log["entries"]:
                # Find the index in grouped_entries that matches this value
                value = log["entries"][field]
                for idx, grouped_value in grouped_entries[field].items():
                    if grouped_value == value:
                        shared_entries[field] = idx
                        break
                # Remove from entries since it's now in shared_entries
                del log["entries"][field]

        # Only add shared_entries if we have any
        if shared_entries:
            log["shared_entries"] = shared_entries

    return logs_out, grouped_entries


def _get_all_filtered_log_event_ids(
    request_fastapi: Request,
    project_name: str,
    context: Optional[str],
    filter_expr: Optional[str],
    from_ids: Optional[str],
    exclude_ids: Optional[str],
    project_dao: ProjectDAO,
    context_dao: ContextDAO,
    field_type_dao: FieldTypeDAO,
    session=Depends(get_db_session),
    as_subquery: bool = False,
) -> Union[Tuple[List[int], int], Tuple[Subquery, int]]:
    """
    Return all log_event_ids (no pagination, no field-level filtering) that match
    these top-level filters: from_ids, exclude_ids, filter_expr, context, and project.

    Returns:
        (event_ids, total_count)
    """
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # Validate project
    try:
        project_obj = project_dao.get_by_user_and_name(
            name=project_name,
            user_id=user_id,
            organization_id=organization_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_name} not found.",
        )

    # Start from LogEvent table
    log_event_query = session.query(LogEvent.id).filter(
        LogEvent.project_id == project_id,
    )

    # Handle from_ids vs exclude_ids
    if from_ids and exclude_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )

    if from_ids:
        include_ids = [int(x) for x in from_ids.split("&")]
        log_event_query = log_event_query.filter(LogEvent.id.in_(include_ids))
    elif exclude_ids:
        exclude_set = [int(x) for x in exclude_ids.split("&")]
        log_event_query = log_event_query.filter(LogEvent.id.notin_(exclude_set))

    context_name = "" if not context else context
    context_obj = context_dao.filter(name=context_name, project_id=project_id)
    if context_obj:
        context_id = context_obj[0][0].id
    else:
        context_id = None
    field_types = field_type_dao.get_field_types(project_id, context_id=context_id)
    # Handle user-defined filter_expr => build SQL expression on LogEvent
    if filter_expr:
        filter_dict = str_filter_exp_to_dict(
            filter_expr,
            field_names=list(field_types.keys()),
        )
        if filter_dict:
            event_ids_subq = log_event_query.subquery(name="event_ids_subq")
            condition = build_sql_query(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=event_ids_subq,
                project_id=project_id,
                context_id=context_id,
            )
            if isinstance(condition, Subquery):
                # Subquery => filter by log_event_id where the value is truthy
                # Use proper truthiness evaluation, not just is_(True)
                from orchestra.web.api.log.python2SQL.operators import (
                    _create_truthiness_condition,
                )

                truthiness_clause = _create_truthiness_condition(condition, session)
                log_event_query = log_event_query.filter(
                    LogEvent.id.in_(
                        select(condition.c.log_event_id).where(truthiness_clause),
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

                condition = _create_truthiness_condition_jsonb(
                    condition,
                    session,
                    project_id,
                    context_id,
                )
                log_event_query = log_event_query.filter(condition)

    # Filter by "static context"
    if context:
        context_obj = context_dao.filter(name=context, project_id=project_id)
    else:
        # use the default context
        context_obj = context_dao.filter(name="", project_id=project_id)
        if not context_obj:
            # no logs present within this context, return empty logs
            return [], 0

    if not context_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context}' not found",
        )
    context_obj = context_obj[0][0]
    ctx_id = context_obj.id
    if ctx_id:
        log_event_query = log_event_query.filter(
            exists(
                select(1)
                .select_from(LogEventContext)
                .where(
                    and_(
                        LogEventContext.log_event_id == LogEvent.id,
                        LogEventContext.context_id == ctx_id,
                    ),
                ),
            ),
        )

    # Get the total count
    total_count = log_event_query.count()

    if as_subquery:
        # Return the query as a subquery without materializing it
        return log_event_query.subquery(name="filtered_event_ids"), total_count
    else:
        # Execute the query: we get all relevant event IDs (no limit/offset)
        all_ids = log_event_query.all()  # each row is a tuple (id,)
        event_ids = [r[0] for r in all_ids]
        return event_ids, total_count


def _fetch_logs_for_event_ids(
    request_fastapi: Request,
    event_ids: Union[List[int], Subquery],
    project_id: int,
    column_context: Optional[str],
    context: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    sorting: Optional[str],
    limit: Optional[int],
    offset: int,
    parent_fields: Optional[str],
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session=Depends(get_db_session),
    latest_timestamp: bool = False,
):
    """
    Given a known list of event_ids, retrieve the union of Log + DerivedLog rows
    that match column_context, from_fields/exclude_fields, etc. Then apply sorting
    + pagination to the distinct event_ids, and return (rows, count).
    If latest_timestamp=True, return only the max updated_at across those logs.
    """
    if isinstance(event_ids, list):
        if not event_ids:
            return ([], 0) if not latest_timestamp else None
    else:
        if not session.query(event_ids.c.id).limit(1).first():
            return ([], 0) if not latest_timestamp else None

    if isinstance(event_ids, list):
        event_ids_cte = (
            session.query(LogEvent.id.label("id"))
            .filter(LogEvent.id.in_(event_ids))
            .cte("event_ids_cte")
        )
    else:
        event_ids_cte = event_ids  # already a sub‑query with "id"

    context_name = "" if not context else context
    ctx_id = (
        context_dao.filter(name=context_name, project_id=project_id)[0][0].id
        if context_name or context_name == ""
        else None
    )
    field_types = field_type_dao.get_field_types(project_id, context_id=ctx_id)

    sort_val_sqs: List[Subquery] = []
    sort_criteria: List[Any] = []

    # Only build unified logs for sorting if we have sorting criteria
    if sorting:
        unified_logs_for_sort = _build_unified_logs_subquery(
            session=session,
            relevant_log_events=event_ids_cte,
        )

        sort_dict = json.loads(sorting)
        for sort_key, mode in sort_dict.items():
            if is_image_field(sort_key, field_types):
                continue
            if mode not in ("ascending", "descending"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Sort mode must be 'ascending' or 'descending', got {mode}",
                )

            cast_expr = _build_sort_criteria(
                unified_logs_for_sort.c.value,
                sort_key,
                field_types,
            )

            sort_sq = (
                select(
                    unified_logs_for_sort.c.log_event_id.label("log_event_id"),
                    cast_expr.label("val"),
                )
                .where(unified_logs_for_sort.c.key == sort_key)
                .order_by(
                    unified_logs_for_sort.c.log_event_id,
                    unified_logs_for_sort.c.updated_at.desc(),
                )
                .distinct(unified_logs_for_sort.c.log_event_id)
                .subquery(f"sort_{sort_key}_sq")
            )
            sort_val_sqs.append(sort_sq)
            direction = asc if mode == "ascending" else desc
            sort_criteria.append(direction(sort_sq.c.val).nulls_last())

    # deterministic tie‑breaker
    sort_criteria.append(desc(event_ids_cte.c.id))

    # Build pagination query
    if sorting and sort_val_sqs:
        # join the sort sub‑queries before pagination
        joined = event_ids_cte
        for sq in sort_val_sqs:
            joined = joined.outerjoin(sq, sq.c.log_event_id == event_ids_cte.c.id)

        pag_query = (
            session.query(
                event_ids_cte.c.id.label("id"),
                func.row_number().over(order_by=sort_criteria).label("row_num"),
            )
            .select_from(joined)
            .order_by(*sort_criteria)
        )
    else:
        # No sorting, simple pagination
        pag_query = session.query(
            event_ids_cte.c.id.label("id"),
            func.row_number().over(order_by=desc(event_ids_cte.c.id)).label("row_num"),
        ).order_by(desc(event_ids_cte.c.id))

    # Get total count before applying limit/offset
    if isinstance(event_ids, list):
        total_count = len(event_ids)
    else:
        total_count = session.query(func.count()).select_from(event_ids_cte).scalar()

    if limit:
        pag_query = pag_query.limit(limit)
    if offset:
        pag_query = pag_query.offset(offset)

    paginated_ids_subq = pag_query.subquery("paginated_ids_subq")

    if latest_timestamp:
        # Build unified logs only for timestamp check
        unified_logs_for_timestamp = _build_unified_logs_subquery(
            session=session,
            relevant_log_events=paginated_ids_subq,
        )
        max_ts = session.query(
            func.max(unified_logs_for_timestamp.c.updated_at),
        ).scalar()
        return max_ts.isoformat() if max_ts else None

    # Build unified logs ONLY for the paginated IDs
    unified_logs_limited = _build_unified_logs_limited(
        session,
        paginated_ids_subq,
    )

    exclude_params = exclude_entries = False
    context_len = 0
    if column_context:
        parts = column_context.split("/")
        exclude_params = "entries" in parts
        exclude_entries = "params" in parts
        real_prefix = "/".join([p for p in parts if p not in ("entries", "params")])
        if real_prefix and not real_prefix.endswith("/"):
            real_prefix += "/"
        context_len = len(real_prefix)
    else:
        real_prefix = ""

    filtered_q = session.query(unified_logs_limited)
    if real_prefix:
        filtered_q = filtered_q.filter(
            unified_logs_limited.c.key.startswith(real_prefix),
        )
    if exclude_params:
        filtered_q = filtered_q.filter(unified_logs_limited.c.param_version.is_(None))
    elif exclude_entries:
        filtered_q = filtered_q.filter(unified_logs_limited.c.param_version.isnot(None))

    if from_fields and exclude_fields:
        raise HTTPException(400, "Cannot set both from_fields and exclude_fields.")
    if from_fields:
        filtered_q = filtered_q.filter(
            unified_logs_limited.c.key.in_(from_fields.split("&")),
        )
    elif exclude_fields:
        filtered_q = filtered_q.filter(
            unified_logs_limited.c.key.notin_(exclude_fields.split("&")),
        )
    if parent_fields:
        filtered_q = filtered_q.filter(
            unified_logs_limited.c.key.notin_(parent_fields.split("&")),
        )

    # Materialize as CTE to help PostgreSQL optimize the join with paginated_ids_subq
    # This prevents PostgreSQL from treating filtered_logs_subq as a correlated subquery
    # and encourages a hash join instead of a nested loop join
    filtered_logs_subq = filtered_q.cte("filtered_logs_subq").prefix_with(
        "MATERIALIZED",
    )

    # Get final logs - total_count already calculated above
    raw_rows = _get_final_logs(session, filtered_logs_subq, paginated_ids_subq)

    results = [
        (
            row_key,
            row_value,
            row_inferred_type,
            row_param_version,
            row_context_version,
            row_source_type,
            row_created_at,
            row_event_id,
        )
        for (
            _id,
            row_event_id,
            row_key,
            row_value,
            row_inferred_type,
            row_param_version,
            row_context_version,
            row_created_at,
            row_source_type,
        ) in raw_rows
    ]

    return results, context_len, total_count


def parse_group_key(key: str) -> Tuple[str, str]:
    """
    Parse a group key into prefix and raw key components.

    Args:
        key: The full group key (e.g., "entries/score", "derived_entries/computed")

    Returns:
        Tuple of (prefix, raw_key) where prefix is one of ["entries", "derived_entries"]
        and raw_key is the actual field name stored in the database.
    """
    parts = key.split("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", key)


def _handle_group_depth_level(
    session,
    log_event_ids: Union[List[int], Subquery],
    field_types,
    group_by,
    group_sorting,
    group_limit,
    group_offset,
    level,
):
    # JSONB mode - query LogEvent.data directly
    return _handle_group_depth_level(
        session=session,
        log_event_ids=log_event_ids,
        field_types=field_types,
        group_by=group_by,
        group_sorting=group_sorting,
        group_limit=group_limit,
        group_offset=group_offset,
        level=level,
    )


def _build_grouping_sets_query(
    session,
    event_ids_cte: Union[CTE, Subquery],
    group_by: List[str],
    group_depth: int,
    field_types: Optional[Dict[str, str]] = None,
    group_sorting: Optional[str] = None,
    group_limit: Optional[int] = None,
    group_offset: int = 0,
):
    """
    Build a single SQL query using GROUPING SETS to get all group counts.

    This eliminates the N+1 recursive query pattern by computing all group
    levels in a single query using PostgreSQL's GROUPING SETS feature.

    Args:
        session: Database session
        event_ids_cte: CTE/subquery containing filtered event IDs
        group_by: List of group keys (e.g., ["entries/_/status", "entries/_/priority"])
        group_depth: Maximum depth to group (0-indexed, e.g., 1 means 2 levels)
        field_types: Field type mapping for casting. Used for type-aware aggregation
            (e.g., casting numeric fields for sorting by mean/sum).
        group_sorting: Optional JSON string with per-level sorting configuration.
            Format: {"entries/status": {"field": "score", "metric": "mean", "direction": "descending"}}
        group_limit: Optional pagination limit for groups
        group_offset: Pagination offset for groups (default 0)

    Returns:
        SQLAlchemy Query object that returns flat rows with:
        - level_0, level_1, ..., level_N: Group values at each level
        - grouping_id: Bitmask identifying which level (0=deepest, higher=subtotals)
        - count: Event count for this group
        - agg_0, agg_1, ... (if sorting): Aggregate values for sorting
        - rn (if pagination): Row number within each grouping level

    Example output for group_by=["status", "priority"], group_depth=1:
        level_0  | level_1 | grouping_id | count
        ---------+---------+-------------+-------
        Open     | NULL    | 1           | 1000   -- Level 0: total for "Open"
        Open     | High    | 0           | 600    -- Level 1: "Open" → "High"
        Closed   | NULL    | 1           | 500

    Example SQL generated with sorting:
        SELECT data->>'status' AS level_0,
               data->>'priority' AS level_1,
               GROUPING(data->>'status', data->>'priority') AS grouping_id,
               COUNT(*) AS count,
               AVG((data->>'score')::float) AS agg_0
        FROM log_event
        WHERE id IN (SELECT id FROM event_ids_cte)
        GROUP BY GROUPING SETS (
            (data->>'status'),
            (data->>'status', data->>'priority')
        )
        ORDER BY grouping_id DESC, agg_0 DESC NULLS LAST, level_0 NULLS LAST
    """
    field_types = field_types or {}

    # Determine how many keys to include (up to group_depth + 1)
    num_keys = min(len(group_by), group_depth + 1)

    # Parse group keys and build JSONB extraction expressions
    # We build two lists:
    # - group_exprs_labeled: labeled expressions for SELECT (level_0, level_1, ...)
    # - group_exprs_raw: unlabeled expressions for GROUPING() and GROUPING SETS
    group_exprs_labeled = []
    group_exprs_raw = []
    raw_keys = []  # Store raw keys for field_types lookup
    for i in range(num_keys):
        current_group_key = group_by[i]
        prefix, raw_key = parse_group_key(current_group_key)
        raw_keys.append(raw_key)

        # Reject param versioning in JSONB mode
        if prefix == "params":
            raise HTTPException(
                status_code=400,
                detail="Parameter versioning is not supported in JSONB mode. "
                "Use entries/ prefix or omit prefix.",
            )

        # Build JSONB extraction expression: data->>'field_name'
        raw_expr = LogEvent.data.op("->>")(raw_key)
        group_exprs_raw.append(raw_expr)
        group_exprs_labeled.append(raw_expr.label(f"level_{i}"))

    # Build GROUPING SETS tuples using raw (unlabeled) expressions
    # For group_depth=1 with keys [status, priority]:
    # GROUPING SETS ((data->>'status'), (data->>'status', data->>'priority'))
    # Use SQLAlchemy's tuple_() to properly construct each grouping set
    grouping_sets_tuples = []
    for depth in range(num_keys):
        # Each tuple contains expressions from level 0 to current depth
        exprs_for_level = group_exprs_raw[: depth + 1]
        # Use tuple_() to wrap expressions as a proper SQL tuple
        grouping_sets_tuples.append(tuple_(*exprs_for_level))

    # Build the SELECT columns
    select_columns = list(group_exprs_labeled)  # level_0, level_1, ...

    # Add GROUPING() function to identify which level each row belongs to
    # GROUPING(expr1, expr2, ...) returns a bitmask where:
    # - bit is 1 if the column is aggregated (NULL in result)
    # - bit is 0 if the column is part of the current grouping
    # Reuse the raw expressions instead of rebuilding them
    select_columns.append(func.grouping(*group_exprs_raw).label("grouping_id"))

    # Add COUNT for aggregation
    select_columns.append(func.count().label("count"))

    # Parse sorting configuration and build aggregate columns
    sort_configs_by_level: Dict[int, SortConfig] = {}
    if group_sorting:
        try:
            parsed_sorting = json.loads(group_sorting)
            for i, group_key in enumerate(group_by[:num_keys]):
                if group_key in parsed_sorting:
                    sort_configs_by_level[i] = SortConfig(**parsed_sorting[group_key])
        except (JSONDecodeError, ValidationError):
            pass

    # Build aggregate columns for sorting
    agg_columns = []
    for level_idx in range(num_keys):
        sort_config = sort_configs_by_level.get(level_idx)
        if sort_config and sort_config.sort_type == SortType.SORT_GROUPS:
            if not sort_config.metric:
                raise HTTPException(
                    status_code=400,
                    detail=f"metric required for sort_groups: {group_by[level_idx]}",
                )

            # Get the field to aggregate for sorting
            _, agg_field_key = parse_group_key(sort_config.field)

            # Extract aggregation field
            agg_expr_raw = LogEvent.data.op("->>")(agg_field_key)

            # Cast based on field type - for mean/sum/min/max, always cast to Float
            # since these operations require numeric types
            field_type = field_types.get(agg_field_key, "str")
            needs_numeric = sort_config.metric in ("mean", "sum", "min", "max")
            if needs_numeric or field_type in ("float", "int"):
                agg_expr_cast = cast(agg_expr_raw, Float)
            else:
                agg_expr_cast = agg_expr_raw

            # Apply aggregation function
            if sort_config.metric == "mean":
                agg_col = func.avg(agg_expr_cast).label(f"agg_{level_idx}")
            elif sort_config.metric == "sum":
                agg_col = func.sum(agg_expr_cast).label(f"agg_{level_idx}")
            elif sort_config.metric == "min":
                agg_col = func.min(agg_expr_cast).label(f"agg_{level_idx}")
            elif sort_config.metric == "max":
                agg_col = func.max(agg_expr_cast).label(f"agg_{level_idx}")
            else:
                agg_col = func.count(agg_expr_cast).label(f"agg_{level_idx}")

            select_columns.append(agg_col)
            agg_columns.append((level_idx, sort_config))

    # Build the base query
    query = (
        session.query(*select_columns)
        .filter(LogEvent.id.in_(select(event_ids_cte.c.id)))
        .group_by(func.grouping_sets(*grouping_sets_tuples))
    )

    # Build ORDER BY clause
    order_by_exprs = []

    # If sorting is configured, order by aggregates
    if agg_columns:
        # First, order by grouping_id DESC (subtotals first, then details)
        order_by_exprs.append(desc("grouping_id"))

        # Then order by aggregate columns
        for level_idx, sort_config in agg_columns:
            if sort_config.direction == SortDirection.ASCENDING:
                order_by_exprs.append(asc(f"agg_{level_idx}").nulls_last())
            else:
                order_by_exprs.append(desc(f"agg_{level_idx}").nulls_last())

        # Finally, order by level columns for deterministic ordering
        for i in range(num_keys):
            order_by_exprs.append(asc(f"level_{i}").nulls_last())
    else:
        # Default: order by level columns for stable ordering
        for i in range(num_keys):
            order_by_exprs.append(asc(f"level_{i}").nulls_last())

    query = query.order_by(*order_by_exprs)

    # Note: Null handling (events missing the grouping key) is handled at the
    # reconstruction level, not via UNION ALL, to maintain compatibility with
    # the recursive path and avoid doubling query complexity. The main GROUPING
    # SETS query naturally returns NULL for missing keys within the grouped data.
    # Events completely missing the key are handled separately in the wrapper.

    # Apply pagination using ROW_NUMBER() window function
    if group_limit is not None:
        # Wrap query in CTE and add row numbering
        base_cte = query.cte("base_query")

        # Build window ORDER BY (same as query ORDER BY minus grouping_id)
        window_order_exprs = []
        if agg_columns:
            for level_idx, sort_config in agg_columns:
                if sort_config.direction == SortDirection.ASCENDING:
                    window_order_exprs.append(
                        asc(base_cte.c[f"agg_{level_idx}"]).nulls_last(),
                    )
                else:
                    window_order_exprs.append(
                        desc(base_cte.c[f"agg_{level_idx}"]).nulls_last(),
                    )
        for i in range(num_keys):
            window_order_exprs.append(asc(base_cte.c[f"level_{i}"]).nulls_last())

        # Build ROW_NUMBER() partitioned by grouping_id
        row_num = (
            func.row_number()
            .over(
                partition_by=base_cte.c.grouping_id,
                order_by=window_order_exprs,
            )
            .label("rn")
        )

        # Build all columns from the CTE
        cte_columns = [base_cte.c[col.key] for col in base_cte.c]
        cte_columns.append(row_num)

        # Build ranked query
        ranked_subq = session.query(*cte_columns).subquery("ranked")

        # Select all columns except rn, with pagination filter
        final_columns = [
            ranked_subq.c[col.key] for col in ranked_subq.c if col.key != "rn"
        ]
        query = (
            session.query(*final_columns)
            .filter(ranked_subq.c.rn > group_offset)
            .filter(ranked_subq.c.rn <= group_offset + group_limit)
        )

        # Re-apply ordering on final query
        final_order_exprs = []
        if agg_columns:
            final_order_exprs.append(desc(ranked_subq.c.grouping_id))
            for level_idx, sort_config in agg_columns:
                if sort_config.direction == SortDirection.ASCENDING:
                    final_order_exprs.append(
                        asc(ranked_subq.c[f"agg_{level_idx}"]).nulls_last(),
                    )
                else:
                    final_order_exprs.append(
                        desc(ranked_subq.c[f"agg_{level_idx}"]).nulls_last(),
                    )
        for i in range(num_keys):
            final_order_exprs.append(asc(ranked_subq.c[f"level_{i}"]).nulls_last())

        query = query.order_by(*final_order_exprs)

    return query


def _get_grouping_level(grouping_id: int, num_keys: int) -> int:
    """
    Compute the effective grouping level from a GROUPING() bitmask.

    PostgreSQL's GROUPING(col0, col1, ..., colN) returns a bitmask where:
    - Bit i (from the right, 0-indexed) corresponds to col(N-i)
    - A bit is 1 if that column is being aggregated (not in current grouping)
    - A bit is 0 if that column is part of the current grouping

    For GROUPING SETS ((col0), (col0, col1)):
    - Grouping by (col0) only: col1 is aggregated → GROUPING(col0, col1) = 1 (binary 01)
    - Grouping by (col0, col1): neither aggregated → GROUPING(col0, col1) = 0 (binary 00)

    The effective level (0 = top/parent, higher = deeper/detail) is:
        level = num_keys - 1 - popcount(grouping_id)

    Where popcount counts the number of 1 bits (aggregated columns).

    Args:
        grouping_id: The bitmask from GROUPING() function
        num_keys: Number of grouping columns

    Returns:
        Effective level (0 = top level, num_keys-1 = deepest detail level)

    Examples for num_keys=2:
        grouping_id=0 (binary 00) → popcount=0 → level = 2-1-0 = 1 (details)
        grouping_id=1 (binary 01) → popcount=1 → level = 2-1-1 = 0 (subtotals)
    """
    popcount = bin(grouping_id).count("1")
    return num_keys - 1 - popcount


def _reconstruct_nested_structure(
    rows: List[Any],
    group_by: List[str],
    group_depth: int,
    has_sorting: bool = False,
    total_group_count: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Reconstruct nested JSON structure from flat GROUPING SETS results.

    Converts flat rows from GROUPING SETS query into the nested dictionary
    structure expected by the API. Supports arbitrary nesting depth using
    iterative tree construction from leaves up to root.

    Args:
        rows: Flat rows from GROUPING SETS query with level_0, level_1, ...,
              grouping_id, count attributes. May also have agg_0, agg_1, ... for sorting.
        group_by: Original group_by list (e.g., ["entries/_/status", "entries/_/priority"])
        group_depth: Maximum depth (0-indexed)
        has_sorting: If True, rows contain aggregate columns for sorting.
              Rows are already sorted by SQL ORDER BY, so preserve that order.
        total_group_count: If provided, use this as the total group count
              (for pagination scenarios where len(rows) < total_group_count).

    Returns:
        Nested dict matching existing API format:
        {
          "entries/_/status": {
            "group": [
              {"key": "Open", "value": 1000},  # If depth=0, value is count
              {"key": "Open", "value": {...}}  # If depth>0, nested structure
            ],
            "group_count": 3,
            "count": 1500
          }
        }

    Algorithm (iterative tree construction):
        1. Decode grouping_id bitmask to determine each row's effective level
        2. Separate rows by level using the decoded bitmask
        3. Process levels from deepest (leaves) to shallowest (root)
        4. At each level, build nested structure and attach children from deeper level
        5. Preserve SQL order when has_sorting=True (don't re-sort in Python)

    GROUPING() Bitmask Interpretation:
        PostgreSQL's GROUPING(col0, col1, col2) returns a bitmask:
        - 0 (binary 000): all columns grouped → deepest detail row
        - 1 (binary 001): col2 aggregated → level 1
        - 3 (binary 011): col1,col2 aggregated → level 0 (top)
        The effective level = num_keys - 1 - popcount(grouping_id)
    """
    from collections import defaultdict

    # Determine how many keys we're working with
    num_keys = min(len(group_by), group_depth + 1)

    if not rows:
        # Return empty structure
        top_key = group_by[0]
        return {top_key: {"group": [], "group_count": 0, "count": 0}}

    # Separate rows by their effective level (decoded from grouping_id bitmask)
    rows_by_level: Dict[int, List[Any]] = defaultdict(list)

    for row in rows:
        grouping_id = row.grouping_id
        effective_level = _get_grouping_level(grouping_id, num_keys)
        rows_by_level[effective_level].append(row)

    # Single level grouping (depth=0 or only 1 key)
    if group_depth == 0 or num_keys == 1:
        top_key = group_by[0]
        group_list = []
        total_count = 0

        for row in rows:
            key_value = row.level_0
            key_str = "null" if key_value is None else str(key_value)
            count = row.count

            group_list.append({"key": key_str, "value": count})
            total_count += count

        final_group_count = (
            total_group_count if total_group_count is not None else len(group_list)
        )

        return {
            top_key: {
                "group": group_list,
                "group_count": final_group_count,
                "count": total_count,
            },
        }

    # Multi-level grouping: iterative tree construction from leaves to root
    # Structure: children_at_level[level][parent_key] = list of {"key": ..., "value": ...}
    # Also track counts: counts_at_level[level][parent_key] = total count
    children_at_level: Dict[int, Dict[Tuple, List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list),
    )
    counts_at_level: Dict[int, Dict[Tuple, int]] = defaultdict(
        lambda: defaultdict(int),
    )

    # Process levels from deepest to shallowest
    # Level num_keys-1 is deepest (all columns grouped, detail rows)
    # Level 0 is shallowest (only first column grouped, top-level subtotals)
    deepest_level = num_keys - 1

    for current_level in range(deepest_level, -1, -1):
        level_rows = rows_by_level.get(current_level, [])

        for row in level_rows:
            # Extract key values up to current level
            level_values = []
            for i in range(current_level + 1):
                val = getattr(row, f"level_{i}", None)
                level_values.append(val)

            # This row's key (at current level)
            this_key = level_values[-1] if level_values else None
            this_key_str = "null" if this_key is None else str(this_key)

            # Parent key is all values except the last (empty tuple for level 0)
            parent_key = tuple(level_values[:-1]) if len(level_values) > 1 else ()

            # The key tuple for this node (used to lookup children at next level)
            this_key_tuple = tuple(level_values)

            # Count from this row
            row_count = row.count

            if current_level == deepest_level:
                # Leaf level: value is just the count
                children_at_level[current_level][parent_key].append(
                    {
                        "key": this_key_str,
                        "value": row_count,
                    },
                )
                counts_at_level[current_level][parent_key] += row_count
            else:
                # Intermediate level: value is nested structure with children
                next_level = current_level + 1
                next_level_key = (
                    group_by[next_level] if next_level < len(group_by) else None
                )

                # Get children from the next (deeper) level
                children = children_at_level[next_level].get(this_key_tuple, [])
                child_count = counts_at_level[next_level].get(this_key_tuple, 0)

                if next_level_key and children:
                    # Match recursive path structure: nested values are NEVER wrapped with key
                    # Only the final top-level return wraps with the first key
                    nested_value = {
                        "group": children,
                        "group_count": len(children),
                        "count": child_count,
                    }
                    children_at_level[current_level][parent_key].append(
                        {
                            "key": this_key_str,
                            "value": nested_value,
                        },
                    )
                else:
                    # No children found - use row count directly
                    children_at_level[current_level][parent_key].append(
                        {
                            "key": this_key_str,
                            "value": row_count,
                        },
                    )

                counts_at_level[current_level][parent_key] += row_count

    # Handle case where we have detail rows but no subtotal rows at some levels
    # Build missing intermediate levels from detail data
    for current_level in range(deepest_level - 1, -1, -1):
        if current_level not in rows_by_level or not rows_by_level[current_level]:
            # No subtotal rows at this level - derive from deeper level
            next_level = current_level + 1
            next_level_key = (
                group_by[next_level] if next_level < len(group_by) else None
            )

            # Group children by their parent at current level
            seen_parents: Dict[Tuple, bool] = {}
            for parent_key, children in children_at_level[next_level].items():
                if not parent_key:
                    continue

                # Parent key for current level is all but last element of next level's parent
                current_parent = parent_key[:-1] if len(parent_key) > 1 else ()
                this_key = parent_key[-1] if parent_key else None
                this_key_str = "null" if this_key is None else str(this_key)

                # Avoid duplicates
                if parent_key in seen_parents:
                    continue
                seen_parents[parent_key] = True

                child_count = counts_at_level[next_level].get(parent_key, 0)

                if next_level_key and children:
                    # Match recursive path structure: nested values are NEVER wrapped with key
                    nested_value = {
                        "group": children,
                        "group_count": len(children),
                        "count": child_count,
                    }
                    children_at_level[current_level][current_parent].append(
                        {
                            "key": this_key_str,
                            "value": nested_value,
                        },
                    )
                else:
                    children_at_level[current_level][current_parent].append(
                        {
                            "key": this_key_str,
                            "value": child_count,
                        },
                    )

                counts_at_level[current_level][current_parent] += child_count

    # Build final result from level 0 (top level)
    top_key = group_by[0]
    group_list = children_at_level[0].get((), [])
    total_count = counts_at_level[0].get((), 0)

    # If no groups at level 0, try to get from the data we have
    if not group_list and rows_by_level:
        # Fallback: sum up all counts from whatever level we have
        for level_rows in rows_by_level.values():
            for row in level_rows:
                total_count += row.count

    final_group_count = (
        total_group_count if total_group_count is not None else len(group_list)
    )

    return {
        top_key: {
            "group": group_list,
            "group_count": final_group_count,
            "count": total_count,
        },
    }


def _can_use_grouping_sets(
    group_sorting: Optional[str],
    group_by: List[str],
    group_depth: Optional[int],
    groups_only: bool,
    level: int = 0,
    group_limit: Optional[int] = None,
    group_offset: int = 0,
) -> bool:
    """
    Determine if GROUPING SETS optimization can be used.

    GROUPING SETS optimization is applicable when:
    - We need count-only results (group_depth is set, not None)
    - We don't need actual event IDs (groups_only=False)
    - We're at the top level (level=0, not mid-recursion)
    - Sorting is for groups (SORT_GROUPS), not within groups (WITHIN_GROUPS)
    - Group-level sorting via aggregates is supported
    - Pagination via ROW_NUMBER() is supported

    Args:
        group_sorting: Optional sorting config JSON
        group_by: List of group keys
        group_depth: Maximum depth to group (None means full recursion)
        groups_only: If True, need actual IDs not just counts
        level: Current recursion level (0 = top level)
        group_limit: Pagination limit for groups
        group_offset: Pagination offset for groups

    Returns:
        True if GROUPING SETS can be used, False otherwise

    Limitations:
        - WITHIN_GROUPS sorting not supported (falls back to recursive)
    """
    # Must have group_depth specified (unlimited recursion not supported)
    if group_depth is None:
        return False

    # GROUPING SETS provides counts, not IDs - can't use for groups_only=True
    if groups_only:
        return False

    # Only optimize from top level (level=0)
    # Mid-recursion optimization would require passing partial state
    if level != 0:
        return False

    # Check if sorting is compatible with GROUPING SETS
    # SORT_GROUPS is supported (sorting by aggregates)
    # WITHIN_GROUPS is NOT supported (requires actual log rows)
    if group_sorting:
        try:
            parsed_sorting = json.loads(group_sorting)
            for level_key, sort_config in parsed_sorting.items():
                # Check if sort_type is WITHIN_GROUPS (not supported in GROUPING SETS)
                if sort_config.get("sort_type") == SortType.WITHIN_GROUPS.value:
                    return False
        except (JSONDecodeError, ValidationError):
            # Invalid sorting config - fall back to recursive to be safe
            return False

    # Must have at least one grouping key
    if len(group_by) < 1:
        return False

    # Supports arbitrary depth via iterative tree construction
    # All conditions passed - can use GROUPING SETS optimization
    return True


def _build_grouped_data_with_grouping_sets(
    session,
    event_ids_cte: Union[CTE, Subquery],
    group_by: List[str],
    group_depth: int,
    field_types: Dict[str, str],
    group_sorting: Optional[str] = None,
    group_limit: Optional[int] = None,
    group_offset: int = 0,
) -> Dict[str, Any]:
    """
    Build grouped data using GROUPING SETS optimization.

    This function orchestrates the optimized path for count-only grouping scenarios.
    It uses a single SQL query with GROUPING SETS to compute all group counts
    across multiple levels, then reconstructs the nested JSON structure.

    Supports group-level sorting via aggregates (SORT_GROUPS) and pagination
    via ROW_NUMBER() window function.

    Args:
        session: Database session
        event_ids_cte: CTE/subquery containing filtered event IDs
        group_by: List of group keys (e.g., ["entries/_/status", "entries/_/priority"])
        group_depth: Maximum depth to group (0-indexed, e.g., 1 means 2 levels)
        field_types: Field type mapping for casting
        group_sorting: Optional JSON string with per-level sorting configuration
        group_limit: Optional pagination limit for groups
        group_offset: Pagination offset for groups (default 0)

    Returns:
        Nested dict matching the API format:
        {
          "entries/_/status": {
            "group": [
              {"key": "Open", "value": 1000},  # If depth=0
              {"key": "Open", "value": {...}}  # If depth>0, nested structure
            ],
            "group_count": 3,
            "count": 1500
          }
        }

    Performance:
        This approach executes a single SQL query instead of N+1 recursive queries,
        providing 10-50x speedup for typical scenarios (e.g., ~100ms vs ~4s for 41k events).
    """
    # Determine if sorting is configured
    has_sorting = group_sorting is not None

    # First, get total group count if pagination is enabled
    # This is needed because the paginated query returns fewer rows than total groups
    total_group_count = None
    if group_limit is not None:
        # Build a separate count query to get total distinct groups at level 0
        _, raw_key = parse_group_key(group_by[0])
        count_query = session.query(
            func.count(func.distinct(LogEvent.data.op("->>")(raw_key))),
        ).filter(LogEvent.id.in_(select(event_ids_cte.c.id)))
        total_group_count = count_query.scalar() or 0

    # Build the GROUPING SETS query
    query = _build_grouping_sets_query(
        session=session,
        event_ids_cte=event_ids_cte,
        group_by=group_by,
        group_depth=group_depth,
        field_types=field_types,
        group_sorting=group_sorting,
        group_limit=group_limit,
        group_offset=group_offset,
    )

    # Optional: Capture SQL for performance testing
    try:
        from sqlalchemy import text

        from orchestra.tests.test_log.sql_capture import capture_sql, is_capture_enabled

        if is_capture_enabled():
            pass

            mode = "jsonb_grouping_sets"
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
            capture_sql(
                test_name="grouping_sets_optimization",
                mode=mode,
                operation="grouping_sets_query",
                sql=compiled_sql,
                explain_output=explain_output,
            )
    except (ImportError, Exception):
        # SQL capture not available or failed - continue without it
        pass

    # Execute the query
    rows = query.all()

    # Reconstruct the nested structure from flat results
    result = _reconstruct_nested_structure(
        rows=rows,
        group_by=group_by,
        group_depth=group_depth,
        has_sorting=has_sorting,
        total_group_count=total_group_count,
    )

    return result


def _handle_group_depth_level(
    session,
    log_event_ids: Union[List[int], Subquery],
    field_types: Dict[str, str],
    group_by: List[str],
    group_sorting: Optional[str],
    group_limit: Optional[int],
    group_offset: int,
    level: int,
) -> Dict[str, Any]:
    """
    JSONB implementation of group depth level handling.

    Returns group counts without recursing further (leaf level for group_depth).

    Args:
        session: Database session
        log_event_ids: List of IDs or subquery
        field_types: Field type mapping
        group_by: List of group keys
        group_sorting: Optional sorting config JSON
        group_limit: Max groups to return
        group_offset: Offset for pagination
        level: Current grouping level

    Returns:
        Dict with group structure containing counts only

    Performance:
        - EAV: UNION + row_number() window
        - JSONB: Single GROUP BY on data->>'field'
    """
    current_group_key = group_by[level]
    prefix, raw_key = parse_group_key(current_group_key)

    # Reject param versioning in JSONB mode
    if prefix == "params":
        raise HTTPException(
            status_code=400,
            detail="Parameter versioning is not supported in JSONB mode. "
            "Use entries/ prefix or omit prefix.",
        )

    # Convert to CTE if needed
    if isinstance(log_event_ids, list):
        event_ids_cte = (
            session.query(LogEvent.id.label("id"))
            .filter(LogEvent.id.in_(log_event_ids))
            .cte("event_ids_cte")
        )
    else:
        event_ids_cte = log_event_ids

    # Build GROUP BY query using JSONB extraction
    # SELECT data->>'raw_key' AS group_value, COUNT(DISTINCT id) AS log_count
    # FROM log_event WHERE id IN (...) AND data ? 'raw_key'
    # GROUP BY data->>'raw_key'
    group_value_expr = LogEvent.data.op("->>")(raw_key)

    base_q = (
        session.query(
            group_value_expr.label("group_value"),
            func.count(func.distinct(LogEvent.id)).label("log_count"),
        )
        .filter(LogEvent.id.in_(select(event_ids_cte.c.id)))
        .filter(LogEvent.data.op("?")(raw_key))  # Key exists
        .group_by(group_value_expr)
        .order_by(desc(func.max(LogEvent.id)).nulls_last())
    )

    # Handle aggregator sorting if configured
    group_sort_config = None
    if group_sorting:
        try:
            parsed_sorting = json.loads(group_sorting)
            group_sort_config = SortConfig(**parsed_sorting[current_group_key])
        except (JSONDecodeError, ValidationError, KeyError):
            pass

        # Apply sorting based on aggregation metric
        if group_sort_config and group_sort_config.sort_type == SortType.SORT_GROUPS:
            if not group_sort_config.metric:
                raise HTTPException(
                    status_code=400,
                    detail=f"metric required for sort_groups: {current_group_key}",
                )

            if group_sort_config.field != current_group_key:
                # Sorting by a different field's aggregation
                _, agg_field_key = parse_group_key(group_sort_config.field)

                # Extract aggregation field
                agg_expr_raw = LogEvent.data.op("->>")(agg_field_key)

                # Cast based on field type
                field_type = field_types.get(agg_field_key, "str")
                if field_type in ("float", "int"):
                    agg_expr_cast = cast(agg_expr_raw, Float)
                else:
                    agg_expr_cast = agg_expr_raw

                # Apply aggregation function
                if group_sort_config.metric == "mean":
                    agg_col = func.avg(agg_expr_cast).label("agg")
                elif group_sort_config.metric == "sum":
                    agg_col = func.sum(agg_expr_cast).label("agg")
                elif group_sort_config.metric == "min":
                    agg_col = func.min(agg_expr_cast).label("agg")
                elif group_sort_config.metric == "max":
                    agg_col = func.max(agg_expr_cast).label("agg")
                else:
                    agg_col = func.count(agg_expr_cast).label("agg")

                base_q = base_q.add_columns(agg_col)

                # Apply sort direction
                if group_sort_config.direction == SortDirection.ASCENDING:
                    base_q = base_q.order_by(asc("agg").nulls_last())
                else:
                    base_q = base_q.order_by(desc("agg").nulls_last())
            else:
                # Sorting by the same field we're grouping on
                field_type = field_types.get(raw_key, "str")
                if field_type in ("float", "int"):
                    agg_expr = cast(group_value_expr, Float)
                else:
                    agg_expr = group_value_expr

                if group_sort_config.metric == "mean":
                    agg_col = func.avg(agg_expr).label("agg")
                elif group_sort_config.metric == "sum":
                    agg_col = func.sum(agg_expr).label("agg")
                else:
                    agg_col = func.count(agg_expr).label("agg")

                base_q = base_q.add_columns(agg_col)

                if group_sort_config.direction == SortDirection.ASCENDING:
                    base_q = base_q.order_by(asc("agg").nulls_last())
                else:
                    base_q = base_q.order_by(desc("agg").nulls_last())

    # Add total count using window function to avoid extra query
    base_q = base_q.add_columns(func.count().over().label("total_count"))

    # Apply pagination
    if group_limit is not None:
        base_q = base_q.offset(group_offset).limit(group_limit)

    group_rows = base_q.all()

    # Extract total count from first row
    total_distinct_groups = group_rows[0].total_count if group_rows else 0

    # Build result
    group_list = []
    for row in group_rows:
        group_list.append({"key": str(row.group_value), "value": row.log_count})

    # Handle missing IDs (logs without this key) - single SQL EXCEPT query
    present_ids_q = (
        session.query(LogEvent.id)
        .filter(LogEvent.id.in_(select(event_ids_cte.c.id)))
        .filter(LogEvent.data.op("?")(raw_key))
    ).subquery()
    missing_ids_q = select(event_ids_cte.c.id).except_(select(present_ids_q.c.id))
    missing_ids = [r[0] for r in session.execute(missing_ids_q).fetchall()]

    if missing_ids:
        group_list.append({"key": "null", "value": len(missing_ids)})

    out_dict = {
        "group": group_list,
        "group_count": total_distinct_groups,
        "count": sum(item["value"] for item in group_list),
    }

    if level == 0:
        return {current_group_key: out_dict}
    return out_dict


def _build_grouped_data(
    request_fastapi: Request,
    project_id: int,
    log_event_ids: Subquery,
    field_order_map: Dict[str, int],
    field_types: Dict[str, str],
    group_by: List[str],
    group_depth: Optional[int],
    group_limit: Optional[int],
    group_offset: int,
    group_sorting: Optional[Dict[str, SortConfig]],
    level: int,
    limit: Optional[int],
    offset: int,
    column_context: Optional[str],
    context: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    sorting: Optional[str],
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session=Depends(get_db_session),
    value_limit: Optional[int] = None,
    groups_only: bool = False,
    return_timestamps: bool = False,
    parent_group_key: Optional[str] = "",
) -> Dict[str, Any]:
    """
    SQL-first implementation of multi-level grouping.
    At each level, a SQL query groups the logs, and for each group a subquery retrieves matching log_event_ids.
    At the leaf level, final logs are fetched.
    Performance is improved by minimizing in-memory processing.
    """
    # JSONB mode - query LogEvent.data directly
    return _build_grouped_data(
        request_fastapi=request_fastapi,
        project_id=project_id,
        log_event_ids=log_event_ids,
        field_order_map=field_order_map,
        field_types=field_types,
        group_by=group_by,
        group_depth=group_depth,
        group_limit=group_limit,
        group_offset=group_offset,
        group_sorting=group_sorting,
        level=level,
        limit=limit,
        offset=offset,
        column_context=column_context,
        context=context,
        from_fields=from_fields,
        exclude_fields=exclude_fields,
        sorting=sorting,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
        value_limit=value_limit,
        groups_only=groups_only,
        return_timestamps=return_timestamps,
        parent_group_key=parent_group_key,
    )

    def _fetch_leaf_logs(ids: Subquery) -> Any:
        rows, ctx_len, leaf_count = _fetch_logs_for_event_ids(
            request_fastapi=request_fastapi,
            event_ids=ids,
            project_id=project_id,
            column_context=column_context,
            context=context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            limit=limit,
            offset=offset,
            parent_fields=parent_group_key,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )
        logs_out, _ = _format_flat_logs(rows, ctx_len, value_limit, field_order_map)
        return logs_out

    # Check if log_event_ids is a list or a subquery
    if isinstance(log_event_ids, list):
        total_logs_in_group = len(log_event_ids)
        if total_logs_in_group == 0:
            return {}
    else:
        # For subquery, check if it returns any rows
        count_check = session.query(log_event_ids.c.id).limit(1).all()
        if not count_check:
            return {}

    if level >= len(group_by):
        if groups_only:
            if return_timestamps:
                rows = (
                    session.query(LogEvent.id, LogEvent.created_at)
                    .filter(LogEvent.id.in_(select(log_event_ids)))
                    .all()
                )
                return {
                    row[0]: row[1].isoformat() for row in rows if row[1] is not None
                }
            else:
                if isinstance(log_event_ids, list):
                    return log_event_ids
                else:
                    all_ids = session.query(log_event_ids).all()
                    event_ids = [r[0] for r in all_ids]
                    return event_ids
        return _fetch_leaf_logs(log_event_ids)

    # Special branch for when we've reached the requested group_depth (we simply return the group counts)
    if group_depth is not None and level == group_depth:
        return _handle_group_depth_level(
            session=session,
            log_event_ids=log_event_ids,
            field_types=field_types,
            group_by=group_by,
            group_sorting=group_sorting,
            group_limit=group_limit,
            group_offset=group_offset,
            level=level,
        )

    current_group_key = group_by[level]
    group_sort_config = None
    if group_sorting:
        try:
            parsed_sorting = json.loads(group_sorting)
            group_sort_config = SortConfig(**parsed_sorting[current_group_key])
        except (JSONDecodeError, ValidationError, KeyError):
            pass
        if (
            group_sort_config
            and group_sort_config.sort_type == SortType.SORT_GROUPS
            and not group_sort_config.metric
        ):
            raise HTTPException(
                status_code=400,
                detail=f"metric is required when sort_type is 'sort_groups' for field '{current_group_key}'",
            )
    # Parse the group key to get prefix and raw key
    prefix, raw_key = parse_group_key(current_group_key)

    # Create a CTE from either the list or use the subquery directly
    if isinstance(log_event_ids, list):
        event_ids_cte = (
            session.query(LogEvent.id.label("id"))
            .filter(LogEvent.id.in_(log_event_ids))
            .cte("event_ids_cte")
        )
    else:
        # If log_event_ids is already a subquery, use it directly
        event_ids_cte = log_event_ids

    unified = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=event_ids_cte,
    )

    # Group by value and filter on the raw key
    field_to_compare = (
        unified.c.param_version if prefix == "params" else unified.c.value
    )
    base_q = (
        session.query(
            field_to_compare.label("group_value"),
            func.max(unified.c.log_event_id).label("log_event_id"),
            func.count(func.distinct(unified.c.log_event_id)).label("group_count"),
        )
        .filter(
            unified.c.log_event_id.in_(select(event_ids_cte.c.id)),
            unified.c.key == raw_key,
        )
        .group_by(field_to_compare)
        .order_by(desc("log_event_id").nulls_last())
    )
    # group sorting
    if group_sort_config and group_sort_config.sort_type == SortType.SORT_GROUPS:
        # Create a subquery to get the field to aggregate on
        if group_sort_config.field != current_group_key:
            # Parse the aggregator field to get the raw key
            _, agg_field_raw_key = parse_group_key(group_sort_config.field)

            # Create aliases for the unified logs subquery
            base_alias = aliased(unified, name="base_alias")
            agg_alias = aliased(unified, name="agg_alias")
            # field_type = field_type_dao.get_field_types(project_id, context_id)
            # Build a sub-subquery that combines the group field and aggregator field
            # This ensures we're properly joining the group key with its corresponding aggregator value
            sub_subq = (
                session.query(
                    base_alias.c.log_event_id.label("log_event_id"),
                    base_alias.c.inferred_type.label("inferred_type"),
                    (
                        base_alias.c.value.label("group_key_value")
                        if prefix != "params"
                        else base_alias.c.param_version.label("group_key_value")
                    ),
                    agg_alias.c.value.label("agg_val"),
                )
                .join(
                    agg_alias,
                    and_(
                        base_alias.c.log_event_id == agg_alias.c.log_event_id,
                        agg_alias.c.key == agg_field_raw_key,
                    ),
                )
                .filter(
                    base_alias.c.log_event_id.in_(select(event_ids_cte.c.id)),
                    base_alias.c.key == raw_key,
                )
                .subquery("sub_subq")
            )

            # Build the outer query that groups by the group key value and applies aggregation
            base_q = session.query(
                sub_subq.c.group_key_value.label("group_value"),
                func.count(func.distinct(sub_subq.c.log_event_id)).label("group_count"),
            ).group_by(sub_subq.c.group_key_value)

            # Use .get() with default "float" for numeric aggregation metrics
            # Also convert "Any" type to "float" for numeric aggregations
            agg_field_type = field_types.get(agg_field_raw_key, "float")
            if agg_field_type == "Any" and group_sort_config.metric in (
                AggregationMetric.MEAN,
                AggregationMetric.SUM,
                AggregationMetric.VAR,
                AggregationMetric.STD,
                AggregationMetric.MIN,
                AggregationMetric.MAX,
                AggregationMetric.MEDIAN,
                AggregationMetric.MODE,
            ):
                agg_field_type = "float"
            agg_expr = _get_reduction_expr(
                group_sort_config.metric,
                agg_field_type,
                sub_subq.c.agg_val,
                label="agg",
            )
            # Add the aggregation expression to the query
            base_q = base_q.add_columns(agg_expr)

            # Apply sorting direction with null handling
            if group_sort_config.direction == SortDirection.ASCENDING:
                base_q = base_q.order_by(asc("agg").nulls_last())
            else:
                base_q = base_q.order_by(desc("agg").nulls_last())
        else:
            # If sorting on the same field we're grouping by
            # Use .get() with default "float" for numeric aggregation metrics
            # Also convert "Any" type to "float" for numeric aggregations
            raw_key_type = field_types.get(raw_key, "float")
            if raw_key_type == "Any" and group_sort_config.metric in (
                AggregationMetric.MEAN,
                AggregationMetric.SUM,
                AggregationMetric.VAR,
                AggregationMetric.STD,
                AggregationMetric.MIN,
                AggregationMetric.MAX,
                AggregationMetric.MEDIAN,
                AggregationMetric.MODE,
            ):
                raw_key_type = "float"
            agg_expr = _get_reduction_expr(
                group_sort_config.metric,
                raw_key_type,
                unified.c.value,
                label="agg",
            )
            # Add the aggregation expression to the query
            base_q = base_q.add_columns(agg_expr)

            # Apply sorting direction with null handling
            if group_sort_config.direction == SortDirection.ASCENDING:
                base_q = base_q.order_by(asc("agg").nulls_last())
            else:
                base_q = base_q.order_by(desc("agg").nulls_last())
    else:
        # Default sorting on log event id
        base_q = base_q.order_by(desc("log_event_id").nulls_last())
    # Calculate total distinct group count before applying pagination
    # This ensures group_count is accurate regardless of pagination
    total_distinct_groups = session.query(
        func.count(base_q.subquery().c.group_value),
    ).scalar()

    # Apply pagination to the query
    if group_limit is not None:
        base_q = base_q.offset(group_offset).limit(group_limit)

    group_rows = base_q.all()
    result_dict = {}
    group_list = []

    for row in group_rows:
        group_val = row.group_value
        if prefix == "params":
            field_to_compare = unified.c.param_version
            value_to_compare = group_val
        else:
            field_to_compare = unified.c.value
            value_to_compare = cast(group_val, JSONB)
        # Get log event IDs for this group value using the raw key
        ids_q = session.query(unified.c.log_event_id).filter(
            unified.c.log_event_id.in_(select(event_ids_cte.c.id)),
            unified.c.key == raw_key,
            field_to_compare == value_to_compare,
        )
        subset_ids = [r[0] for r in ids_q.all()]
        substructure = _build_grouped_data(
            request_fastapi=request_fastapi,
            project_id=project_id,
            log_event_ids=subset_ids,
            field_order_map=field_order_map,
            field_types=field_types,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            group_sorting=group_sorting,
            level=level + 1,
            limit=limit,
            offset=offset,
            column_context=column_context,
            context=context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
            groups_only=groups_only,
            return_timestamps=return_timestamps,
            parent_group_key=(
                "&".join([parent_group_key, raw_key]) if parent_group_key else raw_key
            ),
        )

        # Add to group list instead of directly to result_dict
        group_list.append({"key": str(group_val), "value": substructure})
    # find missing IDs (logs that don't have this key)
    present_value_q = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=event_ids_cte,
        key=raw_key,
    )
    missing_ids_q = session.query(event_ids_cte.c.id).filter(
        ~event_ids_cte.c.id.in_(select(present_value_q.c.log_event_id)),
    )
    missing_ids = [row[0] for row in missing_ids_q.all()]
    if missing_ids:
        null_sub = _build_grouped_data(
            request_fastapi=request_fastapi,
            project_id=project_id,
            log_event_ids=missing_ids,
            field_order_map=field_order_map,
            field_types=field_types,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            group_sorting=group_sorting,
            level=level + 1,
            limit=limit,
            offset=offset,
            column_context=column_context,
            context=context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
            groups_only=groups_only,
            return_timestamps=return_timestamps,
            parent_group_key=(
                "&".join([parent_group_key, raw_key]) if parent_group_key else raw_key
            ),
        )
        # Add null group to the group list
        group_list.append({"key": "null", "value": null_sub})
    # Add the group list to the result dictionary
    result_dict["group"] = group_list

    # Use the pre-calculated total distinct groups count
    result_dict["group_count"] = total_distinct_groups
    sub_total = 0

    def _get_count_from_substructure(sub_val: Union[List, Dict, int]) -> int:
        """Helper to recursively get count from a substructure."""
        if isinstance(sub_val, int):
            return sub_val
        elif isinstance(sub_val, list):
            return len(sub_val)
        elif isinstance(sub_val, dict):
            # First check if this dict has a direct count
            if "count" in sub_val:
                return sub_val["count"]
            # Otherwise sum up counts from all non-metadata fields
            total = 0
            # Handle new structure with 'group' field
            if "group" in sub_val and isinstance(sub_val["group"], list):
                for item in sub_val["group"]:
                    if isinstance(item, dict) and "value" in item:
                        total += _get_count_from_substructure(item["value"])
            else:
                # Legacy structure - iterate through keys
                for k, v in sub_val.items():
                    if k not in ("group_count", "count", "group"):
                        total += _get_count_from_substructure(v)
            return total
        else:
            return 0

    # Calculate total count from the group items
    for item in group_list:
        sub_total += _get_count_from_substructure(item["value"])

    result_dict["count"] = sub_total
    # For the top level, include the prefix in the result key
    return {current_group_key: result_dict}


def _build_grouped_data(
    request_fastapi: Request,
    project_id: int,
    log_event_ids: Union[List[int], Subquery],
    field_order_map: Dict[str, int],
    field_types: Dict[str, str],
    group_by: List[str],
    group_depth: Optional[int],
    group_limit: Optional[int],
    group_offset: int,
    group_sorting: Optional[str],
    level: int,
    limit: Optional[int],
    offset: int,
    column_context: Optional[str],
    context: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    sorting: Optional[str],
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session,
    value_limit: Optional[int] = None,
    groups_only: bool = False,
    return_timestamps: bool = False,
    parent_group_key: Optional[str] = "",
) -> Dict[str, Any]:
    """
    SQL-first multi-level grouping using JSONB operators.

    Uses array_agg(DISTINCT LogEvent.id) to fetch all group memberships in a single query, eliminating N+1 patterns.
    """

    # Convert list to subquery if needed
    if isinstance(log_event_ids, list):
        if not log_event_ids:
            return {}
        event_ids_cte = (
            session.query(LogEvent.id.label("id"))
            .filter(LogEvent.id.in_(log_event_ids))
            .cte("event_ids_cte")
        )
    else:
        event_ids_cte = log_event_ids
        # Check if subquery has results
        if not session.query(event_ids_cte.c.id).limit(1).first():
            return {}

    # GROUPING SETS optimization: use single query for count-only scenarios
    # This provides 10-50x speedup over the recursive approach
    if _can_use_grouping_sets(
        group_sorting=group_sorting,
        group_by=group_by,
        group_depth=group_depth,
        groups_only=groups_only,
        level=level,
        group_limit=group_limit,
        group_offset=group_offset,
    ):
        return _build_grouped_data_with_grouping_sets(
            session=session,
            event_ids_cte=event_ids_cte,
            group_by=group_by,
            group_depth=group_depth,
            field_types=field_types,
            group_sorting=group_sorting,
            group_limit=group_limit,
            group_offset=group_offset,
        )

    # Base case: reached end of group_by list
    if level >= len(group_by):
        if groups_only:
            if return_timestamps:
                rows = (
                    session.query(LogEvent.id, LogEvent.created_at)
                    .filter(LogEvent.id.in_(select(event_ids_cte.c.id)))
                    .all()
                )
                return {
                    row[0]: row[1].isoformat() for row in rows if row[1] is not None
                }
            else:
                if isinstance(log_event_ids, list):
                    return log_event_ids
                else:
                    all_ids = session.query(event_ids_cte.c.id).all()
                    return [r[0] for r in all_ids]

        # Fetch leaf logs using JSONB query
        return _fetch_leaf_logs(
            request_fastapi=request_fastapi,
            event_ids=event_ids_cte,
            project_id=project_id,
            column_context=column_context,
            context=context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            limit=limit,
            offset=offset,
            parent_fields=parent_group_key,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
            field_order_map=field_order_map,
            field_types=field_types,
        )

    # Handle group_depth limit
    if group_depth is not None and level == group_depth:
        return _handle_group_depth_level(
            session=session,
            log_event_ids=event_ids_cte,
            field_types=field_types,
            group_by=group_by,
            group_sorting=group_sorting,
            group_limit=group_limit,
            group_offset=group_offset,
            level=level,
        )

    # Parse current group key
    current_group_key = group_by[level]
    prefix, raw_key = parse_group_key(current_group_key)

    # Reject param versioning
    if prefix == "params":
        raise HTTPException(
            status_code=400,
            detail="Parameter versioning is not supported in JSONB mode. "
            "Use entries/ prefix or omit prefix.",
        )

    # Build GROUP BY query using JSONB extraction
    group_value_expr = LogEvent.data.op("->>")(raw_key)

    base_q = (
        session.query(
            group_value_expr.label("group_value"),
            func.count(func.distinct(LogEvent.id)).label("group_count"),
            func.array_agg(func.distinct(LogEvent.id)).label("event_ids"),
        )
        .filter(LogEvent.id.in_(select(event_ids_cte.c.id)))
        .filter(LogEvent.data.op("?")(raw_key))  # Key exists
        .group_by(group_value_expr)
    )

    # Apply group sorting if configured
    group_sort_config = None
    if group_sorting:
        try:
            parsed_sorting = json.loads(group_sorting)
            group_sort_config = SortConfig(**parsed_sorting[current_group_key])
        except (JSONDecodeError, ValidationError, KeyError):
            pass

    if group_sort_config and group_sort_config.sort_type == SortType.SORT_GROUPS:
        if not group_sort_config.metric:
            raise HTTPException(
                status_code=400,
                detail=f"metric required for sort_groups: {current_group_key}",
            )

        # Apply aggregation-based sorting
        if group_sort_config.field != current_group_key:
            # Sorting by a different field's aggregation
            _, agg_field_key = parse_group_key(group_sort_config.field)

            # Extract aggregation field
            agg_expr_raw = LogEvent.data.op("->>")(agg_field_key)

            # Cast based on field type - for mean/sum/min/max, always cast to Float
            # since these operations require numeric types
            field_type = field_types.get(agg_field_key, "str")
            needs_numeric = group_sort_config.metric in ("mean", "sum", "min", "max")
            if needs_numeric or field_type in ("float", "int"):
                agg_expr_cast = cast(agg_expr_raw, Float)
            else:
                agg_expr_cast = agg_expr_raw

            # Apply aggregation function
            if group_sort_config.metric == "mean":
                agg_col = func.avg(agg_expr_cast).label("agg")
            elif group_sort_config.metric == "sum":
                agg_col = func.sum(agg_expr_cast).label("agg")
            elif group_sort_config.metric == "min":
                agg_col = func.min(agg_expr_cast).label("agg")
            elif group_sort_config.metric == "max":
                agg_col = func.max(agg_expr_cast).label("agg")
            else:
                agg_col = func.count(agg_expr_cast).label("agg")

            base_q = base_q.add_columns(agg_col)

            # Apply sort direction
            if group_sort_config.direction == SortDirection.ASCENDING:
                base_q = base_q.order_by(asc("agg").nulls_last())
            else:
                base_q = base_q.order_by(desc("agg").nulls_last())
        else:
            # Sorting by the same field we're grouping on
            field_type = field_types.get(raw_key, "str")
            needs_numeric = group_sort_config.metric in ("mean", "sum", "min", "max")
            if needs_numeric or field_type in ("float", "int"):
                agg_expr = cast(group_value_expr, Float)
            else:
                agg_expr = group_value_expr

            if group_sort_config.metric == "mean":
                agg_col = func.avg(agg_expr).label("agg")
            elif group_sort_config.metric == "sum":
                agg_col = func.sum(agg_expr).label("agg")
            elif group_sort_config.metric == "min":
                agg_col = func.min(agg_expr).label("agg")
            elif group_sort_config.metric == "max":
                agg_col = func.max(agg_expr).label("agg")
            else:
                agg_col = func.count(agg_expr).label("agg")

            base_q = base_q.add_columns(agg_col)

            if group_sort_config.direction == SortDirection.ASCENDING:
                base_q = base_q.order_by(asc("agg").nulls_last())
            else:
                base_q = base_q.order_by(desc("agg").nulls_last())
    else:
        # Default: sort by most recent log_event_id
        base_q = base_q.order_by(desc(func.max(LogEvent.id)).nulls_last())

    # Add total count using window function to avoid extra query
    # This computes the count once during the main query execution
    # Window function is computed BEFORE LIMIT/OFFSET is applied
    base_q = base_q.add_columns(func.count().over().label("total_count"))

    # Apply pagination
    if group_limit is not None:
        base_q = base_q.offset(group_offset).limit(group_limit)

    # Capture SQL for test analysis (if enabled)
    try:
        from sqlalchemy import text

        from orchestra.tests.test_log.sql_capture import (
            capture_sql,
            is_capture_enabled,
            set_test_context,
        )

        if is_capture_enabled():
            pass

            mode = "jsonb"
            # Compile SQL for capture
            compiled_sql = base_q.statement.compile(
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
            # Set context and capture
            test_name = (
                request_fastapi.headers.get("X-Test-Name", "unknown")
                if request_fastapi
                else "group_query"
            )
            set_test_context(
                test_name=test_name,
                filter_expr=f"group_by({current_group_key})",
                mode=mode,
            )
            capture_sql(
                sql=compiled_sql,
                explain_analyze=explain_output,
                filter_expr_override=f"group_by({current_group_key})",
            )
    except ImportError:
        pass  # sql_capture module not available (production environment)
    except Exception:
        pass  # Silently ignore capture errors

    # Execute query
    group_rows = base_q.all()

    # Extract total count from first row (all rows have same total_count due to window function)
    total_distinct_groups = group_rows[0].total_count if group_rows else 0

    # Build result structure
    result_dict = {}
    group_list = []

    for row in group_rows:
        group_val = row.group_value

        # Use pre-fetched event IDs from array_agg (eliminates N+1 queries)
        # event_ids is always present since we always include array_agg in the query.
        # A None value would indicate a query construction bug.
        if row.event_ids is None:
            raise RuntimeError(
                f"event_ids is None for group '{group_val}' - this indicates a bug in "
                "query construction. array_agg should always be included.",
            )
        subset_ids = list(row.event_ids)

        # Recursively build substructure
        substructure = _build_grouped_data(
            request_fastapi=request_fastapi,
            project_id=project_id,
            log_event_ids=subset_ids,
            field_order_map=field_order_map,
            field_types=field_types,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            group_sorting=group_sorting,
            level=level + 1,
            limit=limit,
            offset=offset,
            column_context=column_context,
            context=context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
            groups_only=groups_only,
            return_timestamps=return_timestamps,
            parent_group_key=(
                "&".join([parent_group_key, raw_key]) if parent_group_key else raw_key
            ),
        )

        group_list.append({"key": str(group_val), "value": substructure})

    # Handle missing IDs (logs without this key) - single SQL EXCEPT query
    present_ids_q = (
        session.query(LogEvent.id)
        .filter(LogEvent.id.in_(select(event_ids_cte.c.id)))
        .filter(LogEvent.data.op("?")(raw_key))
    ).subquery()
    missing_ids_q = select(event_ids_cte.c.id).except_(select(present_ids_q.c.id))
    missing_ids = [r[0] for r in session.execute(missing_ids_q).fetchall()]

    if missing_ids:
        null_sub = _build_grouped_data(
            request_fastapi=request_fastapi,
            project_id=project_id,
            log_event_ids=missing_ids,
            field_order_map=field_order_map,
            field_types=field_types,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            group_sorting=group_sorting,
            level=level + 1,
            limit=limit,
            offset=offset,
            column_context=column_context,
            context=context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
            groups_only=groups_only,
            return_timestamps=return_timestamps,
            parent_group_key=(
                "&".join([parent_group_key, raw_key]) if parent_group_key else raw_key
            ),
        )
        group_list.append({"key": "null", "value": null_sub})

    # Add metadata
    result_dict["group"] = group_list
    result_dict["group_count"] = total_distinct_groups

    # Calculate total count
    def _get_count_from_substructure(sub_val: Union[List, Dict, int]) -> int:
        if isinstance(sub_val, int):
            return sub_val
        elif isinstance(sub_val, list):
            return len(sub_val)
        elif isinstance(sub_val, dict):
            if "count" in sub_val:
                return sub_val["count"]
            total = 0
            # Handle nested grouping structure: {"entries/field": {"group": [...], "count": N}}
            for key, val in sub_val.items():
                if isinstance(val, dict):
                    if "count" in val:
                        total += val["count"]
                    elif "group" in val and isinstance(val["group"], list):
                        for item in val["group"]:
                            if isinstance(item, dict) and "value" in item:
                                total += _get_count_from_substructure(item["value"])
            if total > 0:
                return total
            # Fallback: check for direct "group" key
            if "group" in sub_val and isinstance(sub_val["group"], list):
                for item in sub_val["group"]:
                    if isinstance(item, dict) and "value" in item:
                        total += _get_count_from_substructure(item["value"])
            return total
        return 0

    result_dict["count"] = sum(
        _get_count_from_substructure(item["value"]) for item in group_list
    )

    return {current_group_key: result_dict}


def _fetch_leaf_logs(
    request_fastapi: Request,
    event_ids: Subquery,
    project_id: int,
    column_context: Optional[str],
    context: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    sorting: Optional[str],
    limit: Optional[int],
    offset: int,
    parent_fields: Optional[str],
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session,
    value_limit: Optional[int],
    field_order_map: Dict[str, int],
    field_types: Dict[str, str],
) -> List[Dict[str, Any]]:
    """
    JSONB implementation: Fetch leaf logs for grouped data.

    Uses direct JSONB queries instead of EAV joins.
    """
    from .logging_utils import _format_logs

    # Build query for LogEvent with JSONB data
    query = session.query(LogEvent.id, LogEvent.data, LogEvent.created_at).filter(
        LogEvent.id.in_(select(event_ids.c.id)),
    )

    # Apply sorting if specified
    if sorting:
        sort_dict = json.loads(sorting)
        for sort_key, mode in sort_dict.items():
            if mode not in ("ascending", "descending"):
                continue
            # Use '->' operator instead of '->>' to preserve JSONB type for comparison
            # This allows PostgreSQL to compare numbers as numbers, strings as strings, etc.
            # (using '->>' returns TEXT which sorts lexicographically: "-5" < "0")
            sort_expr = LogEvent.data.op("->")(sort_key)
            field_type = field_types.get(sort_key, "")
            # If we know the type explicitly, cast for efficiency
            if field_type in ("float", "int"):
                sort_expr = cast(LogEvent.data.op("->>")(sort_key), Float)
            direction = asc if mode == "ascending" else desc
            query = query.order_by(direction(sort_expr).nulls_last())

    # Apply default ordering
    query = query.order_by(desc(LogEvent.id))

    # Apply pagination
    if limit:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset)

    rows = query.all()

    # Get field types with full metadata
    context_name = "" if not context else context
    context_obj = context_dao.filter(name=context_name, project_id=project_id)
    context_id = context_obj[0][0].id if context_obj else None
    field_types_full = field_type_dao.get_field_types(
        project_id,
        context_id=context_id,
        return_mutable=True,
    )

    # Format using JSONB formatter
    logs_out, _ = _format_logs(
        rows=rows,
        field_types=field_types_full,
        value_limit=value_limit,
        column_context=column_context,
        field_order_map=field_order_map,
    )

    return logs_out
