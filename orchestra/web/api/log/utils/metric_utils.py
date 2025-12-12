import json
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from fastapi import HTTPException
from sqlalchemy import (
    INTEGER,
    TIMESTAMP,
    Date,
    Float,
    String,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    literal_column,
    select,
)
from sqlalchemy.dialects.postgresql import BOOLEAN, JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.models.orchestra_models import (
    DerivedLog,
    Log,
    LogEvent,
    LogEventContext,
    LogEventDerivedLog,
    LogEventLog,
)

from ..python2SQL import build_sql_query, str_filter_exp_to_dict

__all__ = [
    "_resolve_key_specific_filters",
    "_postprocess_aggregator_value",
    "_reduce_shared_value",
    "AggregationMetric",
    "_get_reduction_expr",
    "_build_jsonb_cast_expr",
    "compute_metric_for_key",
    "compute_metric_for_key_jsonb",
    "compute_metric_bulk",
    "compute_metric_bulk_jsonb",
    "_compute_metric_for_key_grouped",
    "_compute_metric_for_key_grouped_jsonb",
]

######################
# Metrics utilities
######################


class AggregationMetric(str, Enum):
    MEAN = "mean"
    VAR = "var"
    STD = "std"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    COUNT = "count"
    MEDIAN = "median"
    MODE = "mode"


def _resolve_key_specific_filters(
    request,
    key: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract key-specific filter_expr, from_ids, and exclude_ids from the request object.

    Args:
        request: The GetLogsMetricRequest object
        key: The field key to extract filters for

    Returns:
        Tuple of (key_filter_expr, key_from_ids, key_exclude_ids)
    """
    # Parse filter_expr if it's a JSON string
    if request.filter_expr is not None and isinstance(request.filter_expr, str):
        if request.filter_expr.strip().startswith("{"):
            request.filter_expr = json.loads(request.filter_expr)

    key_filter_expr = (
        request.filter_expr.get(key)
        if isinstance(request.filter_expr, dict)
        else request.filter_expr
    )

    # Parse from_ids if it's a JSON string
    if request.from_ids is not None and isinstance(request.from_ids, str):
        if request.from_ids.strip().startswith("{"):
            request.from_ids = json.loads(request.from_ids)

    key_from_ids = (
        request.from_ids.get(key)
        if isinstance(request.from_ids, dict)
        else request.from_ids
    )

    # Parse exclude_ids if it's a JSON string
    if request.exclude_ids is not None and isinstance(request.exclude_ids, str):
        if request.exclude_ids.strip().startswith("{"):
            request.exclude_ids = json.loads(request.exclude_ids)

    key_exclude_ids = (
        request.exclude_ids.get(key)
        if isinstance(request.exclude_ids, dict)
        else request.exclude_ids
    )

    return key_filter_expr, key_from_ids, key_exclude_ids


def _postprocess_aggregator_value(
    value: Any,
    metric: str,
    field_type: Optional[str],
) -> Union[float, int, bool, str, None]:
    """
    Post-process an aggregator value based on field type and metric.

    Args:
        value: The raw aggregated value
        metric: The metric that was computed (mean, sum, etc.)
        field_type: The field type from field_types dict

    Returns:
        The processed value with appropriate type
    """
    if metric == "count":
        return int(value or 0)

    if value is None:
        return None

    if not field_type:
        return value

    try:
        # Convert based on the field type
        if field_type == "datetime":
            if metric in ("var", "std"):
                try:
                    return timedelta(seconds=value).__repr__()
                except (OverflowError, ValueError):
                    # Fallback if timedelta overflow occurs
                    return f"{value} seconds"
            try:
                return datetime.fromtimestamp(value).isoformat()
            except (OverflowError, ValueError, OSError):
                # Fallback if timestamp is out of range
                return f"datetime({value})"

        # Handle new data types: time, date, and timedelta
        elif field_type == "time":
            if metric in ("var", "std"):
                # For variance and standard deviation, return as seconds
                return f"{value} seconds"

            # Convert seconds since midnight to time (with validation)
            try:
                seconds = int(value % 86400)  # Ensure within a day
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                secs = seconds % 60
                return time(hours, minutes, secs).strftime("%H:%M:%S")
            except (ValueError, OverflowError, TypeError):
                # Fallback if time conversion fails
                return f"{value % 86400 if isinstance(value, (int, float)) else value} seconds"

        elif field_type == "date":
            if metric in ("var", "std"):
                # For variance and standard deviation, return days
                return f"{value} days"

            # Try converting to date with validation
            try:
                # If it's a timestamp in seconds
                return date.fromtimestamp(value).isoformat()
            except (ValueError, OverflowError, OSError, TypeError):
                # Calculate days since epoch as fallback
                try:
                    days = value / 86400  # seconds to days
                    return f"{days:.2f} days since epoch"
                except (TypeError, ValueError):
                    return f"date({value})"

        elif field_type == "timedelta":
            # Handle potential extremely large values
            try:
                total_seconds = float(value)

                # For very large values, use a simple representation
                if abs(total_seconds) > 100000000:  # ~3 years in seconds
                    days = total_seconds / 86400
                    return f"{days:.2f} days"

                # Otherwise, build ISO 8601 duration
                hours = int(total_seconds // 3600)
                minutes = int((total_seconds % 3600) // 60)
                seconds = total_seconds % 60

                # Build ISO 8601 duration string
                duration = "P"
                days = hours // 24
                if days:
                    duration += f"{days}D"
                    hours %= 24

                # Add time part if there are hours, minutes, or seconds
                if hours or minutes or seconds:
                    duration += "T"
                    if hours:
                        duration += f"{hours}H"
                    if minutes:
                        duration += f"{minutes}M"
                    if seconds:
                        # Handle fractional seconds
                        if seconds == int(seconds):
                            duration += f"{int(seconds)}S"
                        else:
                            duration += f"{seconds:.6g}S"  # :g removes trailing zeros

                # Handle zero duration edge case
                if duration == "P":
                    duration = "PT0S"

                return duration

            except (TypeError, ValueError, OverflowError):
                # If all else fails, return the raw value with units
                return f"{value} seconds"

        if (
            isinstance(value, (int, float))
            and float(value).is_integer()
            and metric in ("sum", "min", "max", "median", "mode")
            and field_type in ("int", "bool", "str")
        ):
            if field_type == "bool" and metric in ("min", "max", "median", "mode"):
                return bool(int(value))
            return int(value)

        return value

    except Exception as e:
        # Final fallback - if any error occurs, return the raw value with type annotation
        return f"{field_type}({value})"


def _reduce_shared_value(values: List[Any]) -> Optional[Any]:
    """
    Check if all values in the list are identical, and if so, return that value.
    Otherwise, return None.

    Args:
        values: List of values to check

    Returns:
        The shared value if all values are identical, otherwise None
    """
    if not values:
        return None

    # Convert all values to their string representation for comparison
    # This handles complex types like dicts and lists
    first_value = values[0]

    # Check if all values are identical to the first value
    if all(v == first_value for v in values):
        return first_value

    return None


def _get_reduction_expr(metric, inferred_type, aggCol, label):
    # Reuse the get_logs_metric logic but for a specific set of log IDs
    reduction_methods = {
        AggregationMetric.COUNT: func.count,
        AggregationMetric.SUM: func.sum,
        AggregationMetric.MEAN: func.avg,
        AggregationMetric.VAR: func.var_pop,
        AggregationMetric.STD: func.stddev_pop,
        AggregationMetric.MIN: func.min,
        AggregationMetric.MAX: func.max,
        AggregationMetric.MEDIAN: func.percentile_cont(0.5).within_group,
        AggregationMetric.MODE: func.mode().within_group,
    }

    # interpret X.c.value depending on X.c.inferred_type.
    if inferred_type in ["list", "dict", "Any"]:
        # Handle JSONB list/dict aggregation using scalar subqueries
        # For "Any" type, we treat it as a list to handle arrays properly
        # This is safe because jsonb_array_elements returns empty for non-arrays
        if inferred_type in ["list", "Any"]:
            elements = func.jsonb_array_elements(cast(aggCol, JSONB)).table_valued(
                "value",
            )
            target_col = elements.c.value
        else:  # dict
            key_values = func.jsonb_each(cast(aggCol, JSONB)).table_valued("value")
            target_col = key_values.c.value

        numeric_col = cast(target_col, Float)

        # Map metric to aggregation function
        if metric == AggregationMetric.COUNT:
            agg_expr = func.count(numeric_col)
        elif metric == AggregationMetric.SUM:
            agg_expr = func.sum(numeric_col)
        elif metric == AggregationMetric.MEAN:
            agg_expr = func.avg(numeric_col)
        elif metric == AggregationMetric.VAR:
            agg_expr = func.var_pop(numeric_col)
        elif metric == AggregationMetric.STD:
            agg_expr = func.stddev_pop(numeric_col)
        elif metric == AggregationMetric.MIN:
            agg_expr = func.min(numeric_col)
        elif metric == AggregationMetric.MAX:
            agg_expr = func.max(numeric_col)
        elif metric == AggregationMetric.MEDIAN:
            agg_expr = func.percentile_cont(0.5).within_group(numeric_col.asc())
        elif metric == AggregationMetric.MODE:
            agg_expr = func.mode().within_group(numeric_col.asc())

        subquery = (
            select(agg_expr)
            .select_from(
                elements if inferred_type in ["list", "Any"] else key_values,
            )
            .scalar_subquery()
        )
        return func.coalesce(subquery, 0).label(label)

    cast_expr = case(
        # Handle NULL values first
        (aggCol.is_(None), literal(None, type_=Float)),
        (
            inferred_type == "bool",
            aggCol.cast(BOOLEAN).cast(INTEGER).cast(Float),
        ),
        (
            inferred_type == "str",
            func.length(cast(aggCol, JSONB)[0].astext).cast(Float),
        ),
        (
            inferred_type == "datetime",
            func.extract("epoch", cast(cast(aggCol, String), TIMESTAMP)).cast(
                Float,
            ),
        ),
        (
            inferred_type == "time",
            # Extract seconds using time-specific casting
            func.mod(
                func.extract(
                    "epoch",
                    func.cast(
                        func.concat(
                            "2000-01-01 ",
                            func.trim(func.cast(aggCol, String), '"'),
                        ),
                        TIMESTAMP,
                    ),
                ),
                86400,
            ).cast(Float),
        ),
        (
            inferred_type == "date",
            # Extract epoch using date-specific casting
            func.extract(
                "epoch",
                func.cast(func.trim(func.cast(aggCol, String), '"'), Date),
            ).cast(Float),
        ),
        (
            inferred_type == "timedelta",
            # Parse ISO 8601 duration format (e.g., "P1DT6H") to seconds
            # This extracts days, hours, minutes, seconds separately and converts to total seconds
            (
                # Days component (86400 seconds per day)
                func.coalesce(
                    func.cast(
                        func.substring(
                            func.trim(func.cast(aggCol, String), '"'),
                            "P([0-9]+)D",
                        ),
                        Float,
                    )
                    * 86400,
                    0,
                )
                +
                # Hours component (3600 seconds per hour)
                func.coalesce(
                    func.cast(
                        func.substring(
                            func.trim(func.cast(aggCol, String), '"'),
                            "T([0-9]+)H",
                        ),
                        Float,
                    )
                    * 3600,
                    0,
                )
                +
                # Minutes component (60 seconds per minute)
                func.coalesce(
                    func.cast(
                        func.substring(
                            func.trim(func.cast(aggCol, String), '"'),
                            "T[0-9]*H?([0-9]+)M",
                        ),
                        Float,
                    )
                    * 60,
                    0,
                )
                +
                # Seconds component
                func.coalesce(
                    func.cast(
                        func.substring(
                            func.trim(func.cast(aggCol, String), '"'),
                            "T[0-9]*H?[0-9]*M?([0-9.]+)S",
                        ),
                        Float,
                    ),
                    0,
                )
            ).cast(Float),
        ),
        (
            inferred_type == "int",
            func.coalesce(
                func.nullif(
                    cast(cast(aggCol, JSONB).op("->>")(0), String),
                    "null",
                ).cast(Float),
                None,
            ).cast(Float),
        ),
        (
            inferred_type == "float",
            func.coalesce(
                func.nullif(
                    cast(cast(aggCol, JSONB).op("->>")(0), String),
                    "null",
                ).cast(Float),
                None,
            ).cast(Float),
        ),
        else_=literal(0, type_=Float),
    )

    if metric in [
        AggregationMetric.SUM,
        AggregationMetric.MEAN,
        AggregationMetric.VAR,
        AggregationMetric.STD,
    ]:
        return func.coalesce(reduction_methods[metric](cast_expr), 0).label(label)
    else:
        return reduction_methods[metric](cast_expr).label(label)


def _build_jsonb_cast_expr(
    key: str,
    field_type: Optional[str],
    log_event_alias,
) -> Any:
    """
    Build a SQLAlchemy CASE expression to cast JSONB field to Float for aggregation.

    Args:
        key: Field name to extract from LogEvent.data
        field_type: Type from FieldType table (str, int, float, bool, list, dict, datetime, time, date, timedelta)
                    When 'Any', uses jsonb_typeof to determine type dynamically.
        log_event_alias: Aliased LogEvent table or LogEvent model

    Returns:
        SQLAlchemy expression that extracts and casts the field value to Float
    """

    # Extract text value for scalar types
    text_value = log_event_alias.data.op("->>")(key)
    # Extract JSONB value for complex types
    jsonb_value = log_event_alias.data.op("->")(key)

    # Handle NULL checks - if key doesn't exist or value is JSON null
    null_check = text_value.is_(None)
    json_null_check = text_value == "null"

    # For "Any" type, use jsonb_typeof to determine type dynamically (like EAV's inferred_type)
    if field_type == "Any" or field_type is None:
        # jsonb_typeof returns: 'number', 'string', 'boolean', 'array', 'object', 'null'
        jsonb_type_expr = func.jsonb_typeof(jsonb_value)

        return case(
            # NULL checks first
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            # Array - return length
            (
                jsonb_type_expr == "array",
                func.jsonb_array_length(cast(jsonb_value, JSONB)).cast(Float),
            ),
            # Object - return key count
            (
                jsonb_type_expr == "object",
                select(func.count())
                .select_from(func.jsonb_object_keys(cast(jsonb_value, JSONB)))
                .scalar_subquery()
                .cast(Float),
            ),
            # Boolean - cast to int (True=1, False=0)
            (
                jsonb_type_expr == "boolean",
                case(
                    (text_value == "true", literal(1, type_=Float)),
                    else_=literal(0, type_=Float),
                ),
            ),
            # String - return string length
            (
                jsonb_type_expr == "string",
                func.char_length(text_value).cast(Float),
            ),
            # Number - cast directly to float
            (
                jsonb_type_expr == "number",
                func.cast(text_value, Float),
            ),
            # Fallback - return 0
            else_=literal(0, type_=Float),
        ).label("value_as_float")

    if field_type == "list":
        # For lists, return the array length
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=func.jsonb_array_length(cast(jsonb_value, JSONB)).cast(Float),
        ).label("value_as_float")

    elif field_type == "dict":
        # For dicts, return the number of keys
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=select(func.count())
            .select_from(func.jsonb_object_keys(cast(jsonb_value, JSONB)))
            .scalar_subquery()
            .cast(Float),
        ).label("value_as_float")

    elif field_type == "bool":
        # For bools, convert to 0/1
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=cast(jsonb_value, BOOLEAN).cast(INTEGER).cast(Float),
        ).label("value_as_float")

    elif field_type == "str":
        # For strings, return the string length
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=func.length(text_value).cast(Float),
        ).label("value_as_float")

    elif field_type == "datetime":
        # For datetime, extract epoch seconds
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=func.extract(
                "epoch",
                func.cast(text_value, TIMESTAMP),
            ).cast(Float),
        ).label("value_as_float")

    elif field_type == "time":
        # For time, extract seconds since midnight
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=func.mod(
                func.extract(
                    "epoch",
                    func.cast(
                        func.concat("2000-01-01 ", text_value),
                        TIMESTAMP,
                    ),
                ),
                86400,
            ).cast(Float),
        ).label("value_as_float")

    elif field_type == "date":
        # For date, extract epoch seconds
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=func.extract(
                "epoch",
                func.cast(text_value, Date),
            ).cast(Float),
        ).label("value_as_float")

    elif field_type == "timedelta":
        # Parse ISO 8601 duration format (e.g., "P1DT6H") to seconds
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=(
                # Days component (86400 seconds per day)
                func.coalesce(
                    func.cast(
                        func.substring(text_value, "P([0-9]+)D"),
                        Float,
                    )
                    * 86400,
                    0,
                )
                +
                # Hours component (3600 seconds per hour)
                func.coalesce(
                    func.cast(
                        func.substring(text_value, "T([0-9]+)H"),
                        Float,
                    )
                    * 3600,
                    0,
                )
                +
                # Minutes component (60 seconds per minute)
                func.coalesce(
                    func.cast(
                        func.substring(text_value, "T[0-9]*H?([0-9]+)M"),
                        Float,
                    )
                    * 60,
                    0,
                )
                +
                # Seconds component
                func.coalesce(
                    func.cast(
                        func.substring(text_value, "T[0-9]*H?[0-9]*M?([0-9.]+)S"),
                        Float,
                    ),
                    0,
                )
            ).cast(Float),
        ).label("value_as_float")

    elif field_type in ("int", "float"):
        # For int/float types - cast directly to float
        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            else_=func.cast(func.nullif(text_value, "null"), Float),
        ).label("value_as_float")

    else:
        # For unrecognized field_type values - use jsonb_typeof for safety
        # This should rarely happen since we handle 'Any' and None above
        jsonb_type_expr = func.jsonb_typeof(jsonb_value)

        return case(
            (null_check, literal(None, type_=Float)),
            (json_null_check, literal(None, type_=Float)),
            # For numbers, cast directly
            (
                jsonb_type_expr == "number",
                func.cast(text_value, Float),
            ),
            # For strings, return length
            (
                jsonb_type_expr == "string",
                func.char_length(text_value).cast(Float),
            ),
            # For everything else, return 0
            else_=literal(0, type_=Float),
        ).label("value_as_float")


def _compute_metric_for_key_grouped(
    key: str,
    metric: str,
    project_obj,
    context_id: Optional[int],
    field_types,
    group_by: Union[str, List[str]],
    key_filter_expr: Optional[str] = None,
    key_from_ids: Optional[str] = None,
    key_exclude_ids: Optional[str] = None,
    session=None,
) -> Dict[str, Any]:
    """
    Compute a metric for a single key, grouped by another field.

    Args:
        key: The field key to compute the metric for
        metric: The metric to compute (mean, sum, etc.)
        project_obj: The project object
        context_id: The context ID
        field_types: Dict of field types
        group_by: Field(s) to group by (string or list of strings)
        key_filter_expr: Key-specific filter expression
        key_from_ids: Key-specific from_ids
        key_exclude_ids: Key-specific exclude_ids
        session: Database session

    Returns:
        Dict mapping group values to computed metric values
    """
    from orchestra.settings import settings

    if settings.use_jsonb_queries:
        return _compute_metric_for_key_grouped_jsonb(
            key,
            metric,
            project_obj,
            context_id,
            field_types,
            group_by,
            key_filter_expr,
            key_from_ids,
            key_exclude_ids,
            session,
        )

    # Handle single string or list of strings for group_by
    if isinstance(group_by, str):
        group_by_fields = [group_by]
    else:
        group_by_fields = group_by

    # Parse group_by fields to determine if they're params
    group_by_info = []
    for field in group_by_fields:
        parts = field.split("/", 1)
        is_param = len(parts) > 1 and parts[0] == "params"
        actual_field = parts[-1]  # Last part is the actual field name
        group_by_info.append((actual_field, is_param))

    # 1) Build initial query to find matching LogEvent IDs (scoped to context when provided)
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)
    if context_id is not None:
        query = query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )

    assert not (key_from_ids and key_exclude_ids), (
        f"Only one of from_ids or exclude_ids can be set for key '{key}', "
        f"but found values {key_from_ids} and {key_exclude_ids}."
    )

    if key_from_ids:
        query = query.where(LogEvent.id.in_([int(i) for i in key_from_ids.split("&")]))
    elif key_exclude_ids:
        query = query.where(
            LogEvent.id.notin_([int(i) for i in key_exclude_ids.split("&")]),
        )

    if key_filter_expr:
        filter_dict = str_filter_exp_to_dict(
            key_filter_expr,
            field_names=list(field_types.keys()),
        )
        if filter_dict:
            event_ids_subq = query.subquery(name="event_ids_subq")
            condition = build_sql_query(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=event_ids_subq,
            )
            if isinstance(condition, Subquery):
                query = query.filter(
                    exists(
                        select(1)
                        .select_from(condition)
                        .where(
                            and_(
                                condition.c.log_event_id == LogEvent.id,
                                condition.c.value.is_(True),
                            ),
                        ),
                    ),
                )
            else:
                query = query.filter(condition)

    # Subquery of filtered LogEvents
    filtered_events_subq = query.subquery()

    # 2) Build subquery for the aggregator key (both base and derived logs)
    agg_log_q = (
        session.query(
            LogEventLog.log_event_id.label("log_event_id"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
        )
        .join(LogEventLog, LogEventLog.log_id == Log.id)
        .filter(Log.key == key)
        .join(LogEvent, LogEventLog.log_event_id == LogEvent.id)
        .filter(LogEvent.project_id == project_obj.id)
    )

    agg_derived_q = (
        session.query(
            LogEventDerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
        )
        .join(LogEventDerivedLog, LogEventDerivedLog.derived_log_id == DerivedLog.id)
        .filter(DerivedLog.key == key)
        .join(LogEvent, LogEventDerivedLog.log_event_id == LogEvent.id)
        .filter(LogEvent.project_id == project_obj.id)
    )

    # Union them for the aggregator key
    agg_logs_subq = agg_log_q.union_all(agg_derived_q).subquery("agg_logs")

    # 3) For each group_by field, build a subquery
    group_subqueries = []

    for idx, (group_field, is_param) in enumerate(group_by_info):
        if is_param:
            # For parameters, use only base logs with version
            group_q = (
                session.query(
                    LogEventLog.log_event_id.label("log_event_id"),
                    Log.param_version.label("value"),
                    literal("int").label("inferred_type"),
                )
                .join(LogEventLog, LogEventLog.log_id == Log.id)
                .filter(Log.key == group_field)
                .join(LogEvent, LogEventLog.log_event_id == LogEvent.id)
                .filter(LogEvent.project_id == project_obj.id)
            )
            group_subq = group_q.subquery(f"group_{idx}")
        else:
            # For non-parameters, union base logs and derived logs
            group_log_q = (
                session.query(
                    LogEventLog.log_event_id.label("log_event_id"),
                    Log.value.label("value"),
                    Log.inferred_type.label("inferred_type"),
                )
                .join(LogEventLog, LogEventLog.log_id == Log.id)
                .filter(Log.key == group_field)
                .join(LogEvent, LogEventLog.log_event_id == LogEvent.id)
                .filter(LogEvent.project_id == project_obj.id)
            )

            group_derived_q = (
                session.query(
                    LogEventDerivedLog.log_event_id.label("log_event_id"),
                    DerivedLog.value.label("value"),
                    DerivedLog.inferred_type.label("inferred_type"),
                )
                .join(
                    LogEventDerivedLog,
                    LogEventDerivedLog.derived_log_id == DerivedLog.id,
                )
                .filter(DerivedLog.key == group_field)
                .join(LogEvent, LogEventDerivedLog.log_event_id == LogEvent.id)
                .filter(LogEvent.project_id == project_obj.id)
            )

            group_subq = group_log_q.union_all(group_derived_q).subquery(f"group_{idx}")

        group_subqueries.append((group_field, group_subq))

    # 4) Build the reduction methods dictionary
    reduction_methods = {
        "count": func.count,
        "sum": func.sum,
        "mean": func.avg,
        "var": func.var_pop,
        "std": func.stddev_pop,
        "min": func.min,
        "max": func.max,
        "median": func.percentile_cont(0.5).within_group,
        "mode": func.mode().within_group,
    }

    # 5) Start building the query with the aggregator key
    X = aliased(agg_logs_subq)

    # JSONB literal null (inline, not parameterised)
    json_null = literal_column("'null'::jsonb", type_=JSONB)

    # Cast expression for the aggregator value
    cast_expr = case(
        (
            X.c.inferred_type == "list",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.jsonb_array_length(cast(X.c.value, JSONB)).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "dict",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=select(func.count())
                .select_from(func.jsonb_object_keys(cast(X.c.value, JSONB)))
                .scalar_subquery()
                .cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "bool",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=X.c.value.cast(BOOLEAN).cast(INTEGER).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "str",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.length(cast(X.c.value, JSONB)[0].astext).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "datetime",
            func.extract(
                "epoch",
                func.cast(
                    func.nullif(cast(X.c.value, String), "null"),
                    TIMESTAMP,
                ),
            ).cast(Float),
        ),
        (
            X.c.inferred_type == "time",
            # Extract seconds using time-specific casting
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.mod(
                    func.extract(
                        "epoch",
                        func.cast(
                            func.concat(
                                "2000-01-01 ",
                                func.trim(func.cast(X.c.value, String), '"'),
                            ),
                            TIMESTAMP,
                        ),
                    ),
                    86400,
                ).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "date",
            # Extract epoch using date-specific casting
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.extract(
                    "epoch",
                    func.cast(
                        func.nullif(func.cast(X.c.value, String), "null"),
                        Date,
                    ),
                ).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "timedelta",
            # Parse ISO 8601 duration format (e.g., "P1DT6H") to seconds
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=(
                    func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "P([0-9]+)D",
                            ),
                            Float,
                        )
                        * 86400,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "T([0-9]+)H",
                            ),
                            Float,
                        )
                        * 3600,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "T[0-9]*H?([0-9]+)M",
                            ),
                            Float,
                        )
                        * 60,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "T[0-9]*H?[0-9]*M?([0-9.]+)S",
                            ),
                            Float,
                        ),
                        0,
                    )
                ).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "float",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.cast(func.nullif(cast(X.c.value, String), "null"), Float),
            ),
        ),
        (
            X.c.inferred_type == "int",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.cast(func.nullif(cast(X.c.value, String), "null"), Float),
            ),
        ),
        else_=literal(0, type_=Float),
    ).label("value_as_float")

    # Also include the raw value for shared value reduction
    raw_value_expr = X.c.value.label("raw_value")

    # Add group columns
    group_columns = []
    group_subqueries_aliases = []
    for idx, (group_field, group_subq) in enumerate(group_subqueries):
        G = aliased(group_subq, name=f"group_{idx}")
        group_subqueries_aliases.append(G)

        # Use the original value without casting
        group_expr = G.c.value.label(f"group_{idx}_val")

        # Add to query
        group_columns.append(group_expr)

    # 6 i) build the base query with the aggregator key
    query = session.query(
        # group columns
        *group_columns,
        # aggregator
        reduction_methods[metric](cast_expr).label("agg_value"),
        # Include raw values for shared value reduction
        func.array_agg(raw_value_expr).label("raw_values"),
    ).select_from(
        X,
    )  # anchor to aggregator subquery X

    # ii) outerjoin with each group subquery
    for G in group_subqueries_aliases:
        query = query.outerjoin(
            G,
            and_(
                G.c.log_event_id == X.c.log_event_id,
                X.c.log_event_id.in_(select(filtered_events_subq.c.id)),
            ),
        )
    # iii) filter by the filtered events
    query = query.filter(
        X.c.log_event_id.in_(select(filtered_events_subq.c.id)),
    )

    # iv) GROUPBY all group columns
    query = query.group_by(*group_columns)

    # 7) Execute the query and build the result dictionary
    rows = query.all()

    # Get the field type for post-processing
    field_type = field_types.get(key)

    # Build the result dictionary
    result = {}

    # For single-level grouping
    if len(group_by_fields) == 1:
        for row in rows:
            group_val = row[0]  # First column is the group value
            agg_value = row[-2]  # Second-to-last column is the aggregated value
            raw_values = row[-1]  # Last column is the array of raw values

            # First check if all values are identical (shared value reduction)
            shared_value = _reduce_shared_value(raw_values)
            result[str(group_val)] = {"shared_value": None, metric: None}
            if shared_value is not None:
                # If we have a shared value, use it directly
                result[str(group_val)]["shared_value"] = shared_value
            else:
                # Otherwise, use the aggregated value
                # Post-process the aggregated value
                processed_value = _postprocess_aggregator_value(
                    agg_value,
                    metric,
                    field_type,
                )
                # Add to result
                result[str(group_val)][metric] = processed_value
    else:
        # For multi-level grouping, build a nested dictionary
        for row in rows:
            # Get all group values except the last one
            current_dict = result
            for i in range(len(group_by_fields) - 1):
                group_val = row[i]
                if group_val not in current_dict:
                    current_dict[str(group_val)] = {}
                current_dict = current_dict[str(group_val)]

            # Add the leaf value with the last group
            last_group_val = row[len(group_by_fields) - 1]
            agg_value = row[-2]  # Second-to-last column is the aggregated value
            raw_values = row[-1]  # Last column is the array of raw values

            # First check if all values are identical (shared value reduction)
            shared_value = _reduce_shared_value(raw_values)
            current_dict[str(last_group_val)] = {"shared_value": None, metric: None}
            if shared_value is not None:
                # If we have a shared value, use it directly
                current_dict[str(last_group_val)]["shared_value"] = shared_value
            else:
                # Otherwise, use the aggregated value
                # Post-process the aggregated value
                processed_value = _postprocess_aggregator_value(
                    agg_value,
                    metric,
                    field_type,
                )
                # Add to the nested dictionary
                current_dict[str(last_group_val)][metric] = processed_value

    return result


def _compute_metric_for_key_grouped_jsonb(
    key: str,
    metric: str,
    project_obj,
    context_id: Optional[int],
    field_types,
    group_by: Union[str, List[str]],
    key_filter_expr: Optional[str] = None,
    key_from_ids: Optional[str] = None,
    key_exclude_ids: Optional[str] = None,
    session=None,
) -> Dict[str, Any]:
    """
    JSONB-based implementation of _compute_metric_for_key_grouped.

    Queries LogEvent.data directly instead of joining EAV tables.
    """
    # Handle single string or list of strings for group_by
    if isinstance(group_by, str):
        group_by_raw = [group_by]
    else:
        group_by_raw = list(group_by)

    # Parse group_by fields to strip prefixes (entries/, derived_entries/, Entries/, etc.)
    # and check for unsupported param versioning
    group_by_fields = []
    for field in group_by_raw:
        # Check for param versioning (no longer supported)
        if field.startswith("params/"):
            raise HTTPException(
                status_code=400,
                detail=f"Parameter versioning is not supported in JSONB mode. "
                f"Use entries/ prefix or omit prefix. Found: '{field}'",
            )
        # Strip common prefixes (entries/, derived_entries/, Entries/)
        # The actual field names in LogEvent.data don't have these prefixes
        parts = field.split("/", 1)
        if len(parts) == 2 and parts[0].lower() in (
            "entries",
            "derived_entries",
        ):
            group_by_fields.append(parts[1])
        else:
            group_by_fields.append(field)

    # 1) Build initial query to find matching LogEvent IDs (scoped to context when provided)
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)
    if context_id is not None:
        query = query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )

    assert not (key_from_ids and key_exclude_ids), (
        f"Only one of from_ids or exclude_ids can be set for key '{key}', "
        f"but found values {key_from_ids} and {key_exclude_ids}."
    )

    if key_from_ids:
        query = query.where(LogEvent.id.in_([int(i) for i in key_from_ids.split("&")]))
    elif key_exclude_ids:
        query = query.where(
            LogEvent.id.notin_([int(i) for i in key_exclude_ids.split("&")]),
        )

    if key_filter_expr:
        filter_dict = str_filter_exp_to_dict(
            key_filter_expr,
            field_names=list(field_types.keys()),
        )
        if filter_dict:
            event_ids_subq = query.subquery(name="event_ids_subq")
            condition = build_sql_query(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=event_ids_subq,
                project_id=project_obj.id,
                context_id=context_id,
            )
            if isinstance(condition, Subquery):
                query = query.filter(
                    exists(
                        select(1)
                        .select_from(condition)
                        .where(
                            and_(
                                condition.c.log_event_id == LogEvent.id,
                                condition.c.value.is_(True),
                            ),
                        ),
                    ),
                )
            else:
                query = query.filter(condition)

    # Subquery of filtered LogEvents
    filtered_events_subq = query.subquery()

    # 2) Build reduction methods dictionary
    reduction_methods = {
        "count": func.count,
        "sum": func.sum,
        "mean": func.avg,
        "var": func.var_pop,
        "std": func.stddev_pop,
        "min": func.min,
        "max": func.max,
        "median": func.percentile_cont(0.5).within_group,
        "mode": func.mode().within_group,
    }

    # 3) Build the aggregation cast expression for the key
    agg_cast_expr = _build_jsonb_cast_expr(key, field_types.get(key), LogEvent)

    # 4) Build group-by expressions (extract values from JSONB)
    # Use -> operator to get JSONB values (not text) so Python converts them properly
    # This ensures consistent string formatting with EAV mode (e.g., "True" not "true", "11.0" not "11")
    group_exprs = [
        LogEvent.data.op("->")(field).label(f"group_{i}_val")
        for i, field in enumerate(group_by_fields)
    ]

    # 5) Build the raw value expression for shared value reduction
    raw_value_expr = LogEvent.data.op("->")(key).label("raw_value")

    # 6) Build the query with grouping
    query = (
        session.query(
            *group_exprs,
            reduction_methods[metric](agg_cast_expr).label("agg_value"),
            func.array_agg(raw_value_expr).label("raw_values"),
        )
        .select_from(LogEvent)
        .filter(LogEvent.id.in_(select(filtered_events_subq.c.id)))
        # Filter to only rows where the aggregation key exists
        .filter(LogEvent.data.op("?")(literal(key)))
        .group_by(*group_exprs)
    )

    # 7) Execute the query and build the result dictionary
    rows = query.all()

    # Get the field type for post-processing
    field_type = field_types.get(key)

    def _normalize_group_key(val) -> str:
        """
        Normalize group key values for consistent formatting across numeric and string types.
        """
        if val is None:
            return "None"
        # For numeric values, convert to float first to match EAV behavior
        # EAV returns "11.0" for integer 11, not "11"
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(float(val))
        # For booleans, capitalize to match Python's str(True) -> "True"
        if isinstance(val, bool):
            return str(val)
        return str(val)

    # Build the result dictionary
    result = {}

    # For single-level grouping
    if len(group_by_fields) == 1:
        for row in rows:
            group_val = row[0]  # First column is the group value
            agg_value = row[-2]  # Second-to-last column is the aggregated value
            raw_values = row[-1]  # Last column is the array of raw values

            # Normalize the group key to match EAV behavior
            group_key = _normalize_group_key(group_val)

            # First check if all values are identical (shared value reduction)
            shared_value = _reduce_shared_value(raw_values)
            result[group_key] = {"shared_value": None, metric: None}
            if shared_value is not None:
                # If we have a shared value, use it directly
                result[group_key]["shared_value"] = shared_value
            else:
                # Otherwise, use the aggregated value
                # Post-process the aggregated value
                processed_value = _postprocess_aggregator_value(
                    agg_value,
                    metric,
                    field_type,
                )
                # Add to result
                result[group_key][metric] = processed_value
    else:
        # For multi-level grouping, build a nested dictionary
        for row in rows:
            # Get all group values except the last one
            current_dict = result
            for i in range(len(group_by_fields) - 1):
                group_val = row[i]
                group_key = _normalize_group_key(group_val)
                if group_key not in current_dict:
                    current_dict[group_key] = {}
                current_dict = current_dict[group_key]

            # Add the leaf value with the last group
            last_group_val = row[len(group_by_fields) - 1]
            last_group_key = _normalize_group_key(last_group_val)
            agg_value = row[-2]  # Second-to-last column is the aggregated value
            raw_values = row[-1]  # Last column is the array of raw values

            # First check if all values are identical (shared value reduction)
            shared_value = _reduce_shared_value(raw_values)
            current_dict[last_group_key] = {"shared_value": None, metric: None}
            if shared_value is not None:
                # If we have a shared value, use it directly
                current_dict[last_group_key]["shared_value"] = shared_value
            else:
                # Otherwise, use the aggregated value
                # Post-process the aggregated value
                processed_value = _postprocess_aggregator_value(
                    agg_value,
                    metric,
                    field_type,
                )
                # Add to the nested dictionary
                current_dict[last_group_key][metric] = processed_value

    return result


def compute_metric_for_key(
    key: str,
    metric: str,
    project_obj,
    context_id: Optional[int],
    field_types,
    key_filter_expr: Optional[str] = None,
    key_from_ids: Optional[str] = None,
    key_exclude_ids: Optional[str] = None,
    session=None,
) -> Union[float, int, bool, str, None]:
    """
    Compute a metric for a single key.

    Args:
        key: The field key to compute the metric for
        metric: The metric to compute (mean, sum, etc.)
        project_obj: The project object
        context_id: The context ID
        field_types: Dict of field types
        key_filter_expr: Key-specific filter expression
        key_from_ids: Key-specific from_ids
        key_exclude_ids: Key-specific exclude_ids
        session: Database session

    Returns:
        The computed metric value
    """
    from orchestra.settings import settings

    if settings.use_jsonb_queries:
        return compute_metric_for_key_jsonb(
            key,
            metric,
            project_obj,
            context_id,
            field_types,
            key_filter_expr,
            key_from_ids,
            key_exclude_ids,
            session,
        )

    # 1) Build initial query to find matching LogEvent IDs (scoped to context when provided)
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)
    if context_id is not None:
        query = query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )

    assert not (key_from_ids and key_exclude_ids), (
        f"Only one of from_ids or exclude_ids can be set for key '{key}', "
        f"but found values {key_from_ids} and {key_exclude_ids}."
    )

    if key_from_ids:
        query = query.where(LogEvent.id.in_([int(i) for i in key_from_ids.split("&")]))
    elif key_exclude_ids:
        query = query.where(
            LogEvent.id.notin_([int(i) for i in key_exclude_ids.split("&")]),
        )

    if key_filter_expr:
        filter_dict = str_filter_exp_to_dict(
            key_filter_expr,
            field_names=list(field_types.keys()),
        )
        if filter_dict:
            event_ids_subq = query.subquery(name="event_ids_subq")
            condition = build_sql_query(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=event_ids_subq,
            )
            if isinstance(condition, Subquery):
                query = query.filter(
                    exists(
                        select(1)
                        .select_from(condition)
                        .where(
                            and_(
                                condition.c.log_event_id == LogEvent.id,
                                condition.c.value.is_(True),
                            ),
                        ),
                    ),
                )
            else:
                query = query.filter(condition)

    # Subquery of filtered LogEvents
    subquery = query.subquery()

    # 2) retrieve rows from Log and DerivedLog for the requested `key`.
    # Base logs
    log_q = (
        session.query(
            LogEventLog.log_event_id.label("log_event_id"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
        )
        .join(LogEventLog, LogEventLog.log_id == Log.id)
        .filter(Log.key == key)
        .join(LogEvent, LogEventLog.log_event_id == LogEvent.id)
        .filter(LogEvent.project_id == project_obj.id)
    )

    # Derived logs
    derived_q = (
        session.query(
            LogEventDerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
        )
        .join(LogEventDerivedLog, LogEventDerivedLog.derived_log_id == DerivedLog.id)
        .filter(DerivedLog.key == key)
        .join(LogEvent, LogEventDerivedLog.log_event_id == LogEvent.id)
        .filter(LogEvent.project_id == project_obj.id)
    )

    # Union them
    logs_or_derived_subq = log_q.union_all(derived_q).subquery()

    # 3) Apply the aggregator (sum, mean, etc.)
    reduction_methods = {
        "count": func.count,
        "sum": func.sum,
        "mean": func.avg,
        "var": func.var_pop,
        "std": func.stddev_pop,
        "min": func.min,
        "max": func.max,
        "median": func.percentile_cont(0.5).within_group,
        "mode": func.mode().within_group,
    }

    # alias logs_or_derived_subq as "X"
    X = aliased(logs_or_derived_subq)
    # columns: X.c.log_event_id, X.c.value, X.c.inferred_type

    # JSONB literal null (inline, not parameterised)
    json_null = literal_column("'null'::jsonb", type_=JSONB)

    # interpret X.c.value depending on X.c.inferred_type.
    cast_expr = case(
        (
            X.c.inferred_type == "list",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.jsonb_array_length(cast(X.c.value, JSONB)).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "dict",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=select(func.count())
                .select_from(func.jsonb_object_keys(cast(X.c.value, JSONB)))
                .scalar_subquery()
                .cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "bool",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=X.c.value.cast(BOOLEAN).cast(INTEGER).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "str",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.length(cast(X.c.value, JSONB)[0].astext).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "datetime",
            func.extract(
                "epoch",
                func.cast(
                    func.nullif(cast(X.c.value, String), "null"),
                    TIMESTAMP,
                ),
            ).cast(Float),
        ),
        (
            X.c.inferred_type == "time",
            # Extract seconds using time-specific casting
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.mod(
                    func.extract(
                        "epoch",
                        func.cast(
                            func.concat(
                                "2000-01-01 ",
                                func.trim(func.cast(X.c.value, String), '"'),
                            ),
                            TIMESTAMP,
                        ),
                    ),
                    86400,
                ).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "date",
            # Extract epoch using date-specific casting
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.extract(
                    "epoch",
                    func.cast(
                        func.nullif(func.cast(X.c.value, String), "null"),
                        Date,
                    ),
                ).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "timedelta",
            # Parse ISO 8601 duration format (e.g., "P1DT6H") to seconds
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=(
                    func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "P([0-9]+)D",
                            ),
                            Float,
                        )
                        * 86400,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "T([0-9]+)H",
                            ),
                            Float,
                        )
                        * 3600,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "T[0-9]*H?([0-9]+)M",
                            ),
                            Float,
                        )
                        * 60,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(X.c.value, String), '"'),
                                "T[0-9]*H?[0-9]*M?([0-9.]+)S",
                            ),
                            Float,
                        ),
                        0,
                    )
                ).cast(Float),
            ),
        ),
        (
            X.c.inferred_type == "float",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.cast(func.nullif(cast(X.c.value, String), "null"), Float),
            ),
        ),
        (
            X.c.inferred_type == "int",
            case(
                (X.c.value.is_(None), None),
                (X.c.value == json_null, None),
                else_=func.cast(func.nullif(cast(X.c.value, String), "null"), Float),
            ),
        ),
        else_=literal(0, type_=Float),
    ).label("value_as_float")

    # Filter the subquery by the log_event_ids that survived above filters
    metric_query = (
        session.query(
            reduction_methods[metric](cast_expr),
        )
        .select_from(X)
        .filter(X.c.log_event_id.in_(select(subquery)))
    )

    reduced_query = metric_query.scalar()

    # Post-process based on field type
    field_type = field_types.get(key)
    processed_value = _postprocess_aggregator_value(
        reduced_query,
        metric,
        field_type,
    )

    return processed_value


def compute_metric_for_key_jsonb(
    key: str,
    metric: str,
    project_obj,
    context_id: Optional[int],
    field_types,
    key_filter_expr: Optional[str] = None,
    key_from_ids: Optional[str] = None,
    key_exclude_ids: Optional[str] = None,
    session=None,
) -> Union[float, int, bool, str, None]:
    """
    JSONB-based implementation of compute_metric_for_key.

    Queries LogEvent.data directly instead of joining EAV tables.
    """
    # Build reduction methods dictionary
    reduction_methods = {
        "count": func.count,
        "sum": func.sum,
        "mean": func.avg,
        "var": func.var_pop,
        "std": func.stddev_pop,
        "min": func.min,
        "max": func.max,
        "median": func.percentile_cont(0.5).within_group,
        "mode": func.mode().within_group,
    }

    # 1) Build initial query to find matching LogEvent IDs (scoped to context when provided)
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)
    if context_id is not None:
        query = query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )

    assert not (key_from_ids and key_exclude_ids), (
        f"Only one of from_ids or exclude_ids can be set for key '{key}', "
        f"but found values {key_from_ids} and {key_exclude_ids}."
    )

    if key_from_ids:
        query = query.where(LogEvent.id.in_([int(i) for i in key_from_ids.split("&")]))
    elif key_exclude_ids:
        query = query.where(
            LogEvent.id.notin_([int(i) for i in key_exclude_ids.split("&")]),
        )

    if key_filter_expr:
        filter_dict = str_filter_exp_to_dict(
            key_filter_expr,
            field_names=list(field_types.keys()),
        )
        if filter_dict:
            event_ids_subq = query.subquery(name="event_ids_subq")
            condition = build_sql_query(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=event_ids_subq,
                project_id=project_obj.id,
                context_id=context_id,
            )
            if isinstance(condition, Subquery):
                query = query.filter(
                    exists(
                        select(1)
                        .select_from(condition)
                        .where(
                            and_(
                                condition.c.log_event_id == LogEvent.id,
                                condition.c.value.is_(True),
                            ),
                        ),
                    ),
                )
            else:
                query = query.filter(condition)

    # Subquery of filtered LogEvents
    filtered_events_subq = query.subquery()

    # 2) Build the cast expression for the key using JSONB
    cast_expr = _build_jsonb_cast_expr(key, field_types.get(key), LogEvent)

    # 3) Build and execute the aggregation query directly on LogEvent
    metric_query = (
        session.query(reduction_methods[metric](cast_expr))
        .select_from(LogEvent)
        .filter(LogEvent.id.in_(select(filtered_events_subq.c.id)))
        # Filter to only rows where the key exists
        .filter(LogEvent.data.op("?")(literal(key)))
    )

    # Capture SQL for test analysis (if enabled)
    try:
        from sqlalchemy import text

        from orchestra.tests.test_log.sql_capture import (
            capture_sql,
            is_capture_enabled,
            set_test_context,
        )

        if is_capture_enabled():
            from orchestra.settings import settings

            mode = "jsonb" if settings.use_jsonb_queries else "eav"
            # Compile SQL for capture
            compiled_sql = metric_query.statement.compile(
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
                test_name="metric_query",
                filter_expr=f"metric({metric}, {key})",
                mode=mode,
            )
            capture_sql(
                sql=compiled_sql,
                explain_analyze=explain_output,
                filter_expr_override=f"metric({metric}, {key})",
            )
    except ImportError:
        pass  # sql_capture module not available (production environment)
    except Exception:
        pass  # Silently ignore capture errors

    reduced_query = metric_query.scalar()

    # Post-process based on field type
    field_type = field_types.get(key)
    processed_value = _postprocess_aggregator_value(
        reduced_query,
        metric,
        field_type,
    )

    return processed_value


def compute_metric_bulk(
    keys: Sequence[str],
    metric: str,
    project_id: int,
    context_id: Optional[int],
    field_types: Dict[str, str],
    filter_expr: Optional[str] = None,
    from_ids: Optional[str] = None,
    exclude_ids: Optional[str] = None,
    session=None,
) -> Dict[str, Union[float, int, bool, str, None]]:
    """
    Compute a metric for multiple keys in a single GROUP BY SQL query.
    Args:
        keys: Sequence of field keys to compute the metric for
        metric: The metric to compute (mean, sum, etc.)
        project_id: The project ID
        field_types: Dict of field types
        filter_expr: Filter expression
        from_ids: IDs to include
        exclude_ids: IDs to exclude
        session: Database session

    Returns:
        Dict mapping keys to their computed metric values
    """
    from orchestra.settings import settings

    if settings.use_jsonb_queries:
        return compute_metric_bulk_jsonb(
            keys,
            metric,
            project_id,
            context_id,
            field_types,
            filter_expr,
            from_ids,
            exclude_ids,
            session,
        )

    if not keys:
        return {}

    # 1) Build initial query to find matching LogEvent IDs (scoped to context when provided)
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_id)
    if context_id is not None:
        query = query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )

    assert not (from_ids and exclude_ids), (
        f"Only one of from_ids or exclude_ids can be set, "
        f"but found values {from_ids} and {exclude_ids}."
    )

    if from_ids:
        query = query.where(LogEvent.id.in_([int(i) for i in from_ids.split("&")]))
    elif exclude_ids:
        query = query.where(
            LogEvent.id.notin_([int(i) for i in exclude_ids.split("&")]),
        )

    if filter_expr:
        filter_dict = str_filter_exp_to_dict(
            filter_expr,
            field_names=list(field_types.keys()),
        )
        if filter_dict:
            event_ids_subq = query.subquery(name="event_ids_subq")
            condition = build_sql_query(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=event_ids_subq,
            )
            if isinstance(condition, Subquery):
                query = query.filter(
                    exists(
                        select(1)
                        .select_from(condition)
                        .where(
                            and_(
                                condition.c.log_event_id == LogEvent.id,
                                condition.c.value.is_(True),
                            ),
                        ),
                    ),
                )
            else:
                query = query.filter(condition)
    # Subquery of filtered LogEvents
    filtered_events_subq = query.subquery()

    # 2) Build queries for Log and DerivedLog tables
    log_q = (
        select(
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
        )
        .where(Log.key.in_(keys))
        .join(LogEventLog, LogEventLog.log_id == Log.id)
        .join(LogEvent, LogEventLog.log_event_id == LogEvent.id)
        .where(LogEvent.project_id == project_id)
        .where(LogEventLog.log_event_id.in_(select(filtered_events_subq.c.id)))
    )

    derived_q = (
        select(
            DerivedLog.key.label("key"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
        )
        .where(DerivedLog.key.in_(keys))
        .join(LogEventDerivedLog, LogEventDerivedLog.derived_log_id == DerivedLog.id)
        .join(LogEvent, LogEventDerivedLog.log_event_id == LogEvent.id)
        .where(LogEvent.project_id == project_id)
        .where(LogEventDerivedLog.log_event_id.in_(select(filtered_events_subq.c.id)))
    )

    # 3) Union the queries to get all entries
    entries = log_q.union_all(derived_q).subquery("entries")

    # 4) Define the cast expression for converting values to float

    # JSONB literal null (inline, not parameterised)
    json_null = literal_column("'null'::jsonb", type_=JSONB)
    cast_expr = case(
        (
            entries.c.inferred_type == "list",
            case(
                (entries.c.value.is_(None), None),
                (entries.c.value == json_null, None),
                else_=func.jsonb_array_length(cast(entries.c.value, JSONB)).cast(Float),
            ),
        ),
        (
            entries.c.inferred_type == "dict",
            case(
                (entries.c.value.is_(None), None),
                (entries.c.value == json_null, None),
                else_=select(func.count())
                .select_from(func.jsonb_object_keys(cast(entries.c.value, JSONB)))
                .scalar_subquery()
                .cast(Float),
            ),
        ),
        (
            entries.c.inferred_type == "bool",
            case(
                (entries.c.value.is_(None), None),
                (entries.c.value == json_null, None),
                else_=entries.c.value.cast(BOOLEAN).cast(INTEGER).cast(Float),
            ),
        ),
        (
            entries.c.inferred_type == "str",
            case(
                (entries.c.value.is_(None), None),
                (entries.c.value == json_null, None),
                else_=func.length(cast(entries.c.value, JSONB)[0].astext).cast(Float),
            ),
        ),
        (
            entries.c.inferred_type == "datetime",
            func.extract(
                "epoch",
                func.cast(
                    func.nullif(cast(entries.c.value, String), "null"),
                    TIMESTAMP,
                ),
            ).cast(Float),
        ),
        (
            entries.c.inferred_type == "time",
            case(
                (entries.c.value.is_(None), None),
                (entries.c.value == json_null, None),
                # Extract seconds using time-specific casting
                else_=func.mod(
                    func.extract(
                        "epoch",
                        func.cast(
                            func.concat(
                                "2000-01-01 ",
                                func.trim(func.cast(entries.c.value, String), '"'),
                            ),
                            TIMESTAMP,
                        ),
                    ),
                    86400,
                ).cast(Float),
            ),
        ),
        (
            entries.c.inferred_type == "date",
            case(
                (entries.c.value.is_(None), None),
                (entries.c.value == json_null, None),
                # Extract epoch using date-specific casting
                else_=func.extract(
                    "epoch",
                    func.cast(
                        func.nullif(func.cast(entries.c.value, String), "null"),
                        Date,
                    ),
                ).cast(Float),
            ),
        ),
        (
            entries.c.inferred_type == "timedelta",
            case(
                (entries.c.value.is_(None), None),
                (entries.c.value == json_null, None),
                # Parse ISO 8601 duration format (e.g. "P1DT6H") to seconds
                else_=(
                    func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(entries.c.value, String), '"'),
                                "P([0-9]+)D",
                            ),
                            Float,
                        )
                        * 86400,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(entries.c.value, String), '"'),
                                "T([0-9]+)H",
                            ),
                            Float,
                        )
                        * 3600,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(entries.c.value, String), '"'),
                                "T[0-9]*H?([0-9]+)M",
                            ),
                            Float,
                        )
                        * 60,
                        0,
                    )
                    + func.coalesce(
                        func.cast(
                            func.substring(
                                func.trim(func.cast(entries.c.value, String), '"'),
                                "T[0-9]*H?[0-9]*M?([0-9.]+)S",
                            ),
                            Float,
                        ),
                        0,
                    )
                ).cast(Float),
            ),
        ),
        (
            entries.c.inferred_type == "float",
            func.cast(
                func.nullif(func.cast(entries.c.value, String), "null"),
                Float,
            ),
        ),
        (
            entries.c.inferred_type == "int",
            func.cast(
                func.nullif(func.cast(entries.c.value, String), "null"),
                Float,
            ),
        ),
        else_=literal(0, type_=Float),
    ).label("value_as_float")

    agg_expr = None
    if metric == AggregationMetric.COUNT:
        agg_expr = func.count(cast_expr)
    elif metric == AggregationMetric.SUM:
        agg_expr = func.sum(cast_expr)
    elif metric == AggregationMetric.MEAN:
        agg_expr = func.avg(cast_expr)
    elif metric == AggregationMetric.VAR:
        agg_expr = func.var_pop(cast_expr)
    elif metric == AggregationMetric.STD:
        agg_expr = func.stddev_pop(cast_expr)
    elif metric == AggregationMetric.MIN:
        agg_expr = func.min(cast_expr)
    elif metric == AggregationMetric.MAX:
        agg_expr = func.max(cast_expr)
    elif metric == AggregationMetric.MEDIAN:
        agg_expr = func.percentile_cont(0.5).within_group(cast_expr.asc())
    elif metric == AggregationMetric.MODE:
        agg_expr = func.mode().within_group(cast_expr.asc())
    else:
        raise ValueError(f"Unsupported metric: {metric}")

    # 6) Build and execute the final query
    query = select(
        entries.c.key.label("key"),
        func.coalesce(agg_expr, 0).label("val"),
    ).group_by(entries.c.key)

    # 7) Execute the query and build the result dictionary
    result = {}
    for row in session.execute(query):
        key = row.key
        value = row.val

        # Post-process the value based on field type
        field_type = field_types.get(key)
        processed_value = _postprocess_aggregator_value(
            value,
            metric,
            field_type,
        )
        result[key] = processed_value

    # 8) Add any missing keys with None values
    for key in keys:
        if key not in result:
            result[key] = None

    return result


def compute_metric_bulk_jsonb(
    keys: Sequence[str],
    metric: str,
    project_id: int,
    context_id: Optional[int],
    field_types: Dict[str, str],
    filter_expr: Optional[str] = None,
    from_ids: Optional[str] = None,
    exclude_ids: Optional[str] = None,
    session=None,
) -> Dict[str, Union[float, int, bool, str, None]]:
    """
    JSONB-based implementation of compute_metric_bulk.

    Queries LogEvent.data directly instead of joining EAV tables.
    Uses a simpler per-key approach to avoid Cartesian product issues.
    """
    if not keys:
        return {}

    # 1) Build initial query to find matching LogEvent IDs (scoped to context when provided)
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_id)
    if context_id is not None:
        query = query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )

    assert not (from_ids and exclude_ids), (
        f"Only one of from_ids or exclude_ids can be set, "
        f"but found values {from_ids} and {exclude_ids}."
    )

    if from_ids:
        query = query.where(LogEvent.id.in_([int(i) for i in from_ids.split("&")]))
    elif exclude_ids:
        query = query.where(
            LogEvent.id.notin_([int(i) for i in exclude_ids.split("&")]),
        )

    if filter_expr:
        filter_dict = str_filter_exp_to_dict(
            filter_expr,
            field_names=list(field_types.keys()),
        )
        if filter_dict:
            event_ids_subq = query.subquery(name="event_ids_subq")
            condition = build_sql_query(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=event_ids_subq,
                project_id=project_id,
                context_id=context_id,
            )
            if isinstance(condition, Subquery):
                query = query.filter(
                    exists(
                        select(1)
                        .select_from(condition)
                        .where(
                            and_(
                                condition.c.log_event_id == LogEvent.id,
                                condition.c.value.is_(True),
                            ),
                        ),
                    ),
                )
            else:
                query = query.filter(condition)

    # Subquery of filtered LogEvents
    filtered_events_subq = query.subquery()

    # 2) Build reduction methods dictionary
    reduction_methods = {
        "count": func.count,
        "sum": func.sum,
        "mean": func.avg,
        "var": func.var_pop,
        "std": func.stddev_pop,
        "min": func.min,
        "max": func.max,
        "median": func.percentile_cont(0.5).within_group,
        "mode": func.mode().within_group,
    }

    # 3) Compute metric for each key individually
    # This avoids Cartesian product issues with UNNEST
    result = {}
    for key in keys:
        # Build the cast expression for this key
        cast_expr = _build_jsonb_cast_expr(key, field_types.get(key), LogEvent)

        # Build and execute the aggregation query
        metric_query = (
            session.query(reduction_methods[metric](cast_expr))
            .select_from(LogEvent)
            .filter(LogEvent.id.in_(select(filtered_events_subq.c.id)))
            # Filter to only rows where the key exists
            .filter(LogEvent.data.op("?")(literal(key)))
        )

        # Capture SQL for test analysis (if enabled)
        try:
            from sqlalchemy import text

            from orchestra.tests.test_log.sql_capture import (
                capture_sql,
                is_capture_enabled,
                set_test_context,
            )

            if is_capture_enabled():
                from orchestra.settings import settings

                mode = "jsonb" if settings.use_jsonb_queries else "eav"
                # Compile SQL for capture
                compiled_sql = metric_query.statement.compile(
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
                    test_name="metric_bulk_query",
                    filter_expr=f"metric_bulk({metric}, {key})",
                    mode=mode,
                )
                capture_sql(
                    sql=compiled_sql,
                    explain_analyze=explain_output,
                    filter_expr_override=f"metric_bulk({metric}, {key})",
                )
        except ImportError:
            pass  # sql_capture module not available (production environment)
        except Exception:
            pass  # Silently ignore capture errors

        value = metric_query.scalar()

        # Post-process the value based on field type
        field_type = field_types.get(key)
        processed_value = _postprocess_aggregator_value(
            value,
            metric,
            field_type,
        )
        result[key] = processed_value

    return result
