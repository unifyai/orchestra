import json
from enum import Enum
from json import JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, asc, cast, desc, exists, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    DerivedLog,
    Log,
    LogEvent,
    LogEventContext,
    LogEventDerivedLog,
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
    "_get_log_event_ids_for_group_value",
    "_get_params_for_log_events",
    "_fetch_logs_for_event_ids",
    "_build_grouped_data",
    "_get_all_filtered_log_event_ids",
    "_handle_group_depth_level",
    "_build_grouped_data",
    "parse_group_key",
    "apply_group_threshold",
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


def _get_distinct_group_values(
    log_event_ids: List[int],
    group_key: str,
    session,
    is_param: bool = False,
    sort_direction: Optional[str] = None,
) -> List[Any]:
    """
    Get distinct values for a group key among provided log event IDs.
    For non-parameter fields (is_param=False), includes both base logs and derived logs.
    For parameters (is_param=True), only includes base logs.
    """
    if is_param:
        # For parameters, use only base logs with version
        value_col = Log.param_version
        subquery = (
            session.query(
                value_col.label("value"),
                LogEventLog.log_event_id,
                func.row_number()
                .over(
                    partition_by=value_col,
                    order_by=desc(LogEventLog.log_event_id),
                )
                .label("rn"),
            )
            .join(LogEventLog, LogEventLog.log_id == Log.id)
            .filter(LogEventLog.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
            .subquery()
        )
    else:
        # For non-parameters, union base logs and derived logs
        base_query = (
            session.query(
                Log.value.label("value"),
                LogEventLog.log_event_id.label("log_event_id"),
            )
            .join(LogEventLog, LogEventLog.log_id == Log.id)
            .filter(LogEventLog.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
        )

        derived_query = (
            session.query(
                DerivedLog.value.label("value"),
                LogEventDerivedLog.log_event_id.label("log_event_id"),
            )
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .filter(LogEventDerivedLog.log_event_id.in_(log_event_ids))
            .filter(DerivedLog.key == group_key)
        )

        # Combine base and derived logs
        combined_query = base_query.union_all(derived_query).subquery(
            name="unified_logs",
        )

        # Apply row_number over the combined results
        subquery = (
            session.query(
                combined_query.c.value,
                combined_query.c.log_event_id,
                func.row_number()
                .over(
                    partition_by=combined_query.c.value,
                    order_by=desc(combined_query.c.log_event_id),
                )
                .label("rn"),
            )
        ).subquery()

    # Get distinct values with configurable ordering
    query = session.query(subquery.c.value).filter(subquery.c.rn == 1)

    if sort_direction == "ascending":
        query = query.order_by(asc(subquery.c.value).nulls_last())
    elif sort_direction == "descending":
        query = query.order_by(desc(subquery.c.value).nulls_first())
    else:
        # Default ordering by log_event_id descending
        query = query.order_by(desc(subquery.c.log_event_id))

    return [row[0] for row in query.all()]


def _get_log_event_ids_for_group_value(
    log_event_ids: List[int],
    group_key: str,
    group_value: Any,
    session,
    is_param: bool = False,
) -> List[int]:
    """
    Get log event IDs that match a specific group value.
    For non-parameter fields (is_param=False), searches both base logs and derived logs.
    For parameters (is_param=True), only searches base logs.
    """
    if is_param:
        # For parameters, only search base logs
        query = (
            session.query(LogEventLog.log_event_id)
            .join(Log, Log.id == LogEventLog.log_id)
            .filter(LogEventLog.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
            .filter(Log.param_version == group_value)
        )
    elif group_key == "derived_entries":
        # For derived entries, only search derived logs
        query = (
            session.query(LogEventDerivedLog.log_event_id)
            .join(DerivedLog, DerivedLog.id == LogEventDerivedLog.derived_log_id)
            .filter(LogEventDerivedLog.log_event_id.in_(log_event_ids))
            .filter(DerivedLog.key == group_key)
            .filter(cast(DerivedLog.value, JSONB) == cast(group_value, JSONB))
        )
    else:
        # For non-parameters, search both base and derived logs
        base_query = (
            session.query(LogEventLog.log_event_id)
            .join(Log, Log.id == LogEventLog.log_id)
            .filter(LogEventLog.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
            .filter(cast(Log.value, JSONB) == cast(group_value, JSONB))
        )

        derived_query = (
            session.query(LogEventDerivedLog.log_event_id)
            .join(DerivedLog, DerivedLog.id == LogEventDerivedLog.derived_log_id)
            .filter(LogEventDerivedLog.log_event_id.in_(log_event_ids))
            .filter(DerivedLog.key == group_key)
            .filter(cast(DerivedLog.value, JSONB) == cast(group_value, JSONB))
        )

        # Combine results from both tables
        query = base_query.union_all(derived_query)

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
    project: str,
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

    # Validate project
    try:
        project_obj = project_dao.get_by_user_and_name(name=project, user_id=user_id)
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(status_code=404, detail=f"Project {project} not found.")

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
            )
            if isinstance(condition, Subquery):
                # Subquery => we check existence
                log_event_query = log_event_query.filter(
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
        key: The full group key (e.g., "entries/score", "params/temperature")

    Returns:
        Tuple of (prefix, raw_key) where prefix is one of ["entries", "params", "derived_entries"]
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

    current_group_key = group_by[level]
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

    # Build unified logs subquery for the current log_event_ids
    unified = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=event_ids_cte,
    )

    # Group by value and filter on the raw key
    field_to_compare = (
        unified.c.param_version if prefix == "params" else unified.c.value
    )
    if isinstance(log_event_ids, list):
        event_ids = log_event_ids
    else:
        event_ids = select(log_event_ids)
    base_q = (
        session.query(
            field_to_compare.label("group_value"),
            func.max(unified.c.log_event_id).label("log_event_id"),
            func.count(func.distinct(unified.c.log_event_id)).label("log_count"),
        )
        .filter(
            unified.c.log_event_id.in_(event_ids),
            unified.c.key == raw_key,
        )
        .group_by(field_to_compare)
        .order_by(desc("log_event_id").nulls_last())
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
            # Create a subquery to get the field to aggregate on
            if group_sort_config.field != current_group_key:
                # Parse the aggregator field to get the raw key
                _, agg_field_raw_key = parse_group_key(group_sort_config.field)

                # Create aliases for the unified logs subquery
                base_alias = aliased(unified, name="base_alias")
                agg_alias = aliased(unified, name="agg_alias")

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
                        base_alias.c.log_event_id.in_(select(log_event_ids)),
                        base_alias.c.key == raw_key,
                    )
                    .subquery("sub_subq")
                )

                # Build the outer query that groups by the group key value and applies aggregation
                base_q = session.query(
                    sub_subq.c.group_key_value.label("group_value"),
                    func.count(func.distinct(sub_subq.c.log_event_id)).label(
                        "group_count",
                    ),
                ).group_by(sub_subq.c.group_key_value)

                # Apply the appropriate aggregation function to the aggregator field
                agg_expr = _get_reduction_expr(
                    group_sort_config.metric,
                    field_types[agg_field_raw_key],
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
                agg_expr = _get_reduction_expr(
                    group_sort_config.metric,
                    field_types[raw_key],
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
    total_distinct_groups = session.query(
        func.count(base_q.subquery().c.group_value),
    ).scalar()

    # Apply pagination to the query
    if group_limit is not None:
        base_q = base_q.offset(group_offset).limit(group_limit)

    # Execute the query
    group_rows = base_q.all()

    # Build the result dictionary with the new structure
    out_dict = {}
    group_list = []

    # Convert rows to array of objects with key/value pairs
    for row in group_rows:
        group_val = row.group_value
        log_count = row.group_count if hasattr(row, "group_count") else row.log_count
        group_list.append({"key": str(group_val), "value": log_count})

    # Find missing IDs (logs that don't have this key)
    present_value_q = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=event_ids_cte,
        key=raw_key,
    )
    missing_ids_q = session.query(event_ids_cte.c.id).filter(
        ~event_ids_cte.c.id.in_(select(present_value_q.c.log_event_id)),
    )
    missing_ids = [row[0] for row in missing_ids_q.all()]

    # Add null group if there are missing IDs
    if missing_ids:
        group_list.append({"key": "null", "value": len(missing_ids)})

    # Add the group list to the output dictionary
    out_dict["group"] = group_list

    # Add metadata
    out_dict["group_count"] = total_distinct_groups
    out_dict["count"] = sum(item["value"] for item in group_list)

    # Wrap in current_group_key if at top level
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

            agg_expr = _get_reduction_expr(
                group_sort_config.metric,
                field_types[agg_field_raw_key],
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
            agg_expr = _get_reduction_expr(
                group_sort_config.metric,
                field_types[raw_key],
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
