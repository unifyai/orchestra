import json
import random
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, HTTPException, Request
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Integer,
    String,
    Text,
    and_,
    asc,
    case,
    cast,
    desc,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import ColumnClause
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Context,
    DerivedLog,
    Embedding,
    JSONLog,
    JSONLogHistory,
    Log,
    LogEvent,
    LogEventContext,
    LogEventDerivedLog,
    LogEventJSONLog,
    LogEventJSONLogHistory,
    LogEventLog,
    Project,
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
    "create_logs_internal",
    "_build_unified_logs_subquery",
    "_flatten_fields",
    "_format_flat_logs",
    "_get_final_logs",
    "is_image_field",
    "is_audio_field",
    "_join_logs",
    "get_or_create_usage_project",
    "log_chat_completion_event",
]


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


def get_or_create_usage_project(
    project_dao: ProjectDAO,
    user_id: str,
) -> Project:
    """
    Get or create the Usage project for a user.

    Args:
        project_dao: The project data access object
        user_id: The ID of the user

    Returns:
        The Project instance for the Usage project
    """
    project = project_dao.get_by_user_and_name(
        user_id,
        settings.chat_completions_project_name,
    )
    if not project:
        project_dao.create(user_id=user_id, name=settings.chat_completions_project_name)
        project_dao.session.commit()
        project = project_dao.get_by_user_and_name(
            user_id,
            settings.chat_completions_project_name,
        )
    return project


def log_chat_completion_event(
    user_id: str,
    session,
    **kwargs,
) -> List[int]:
    """
    Log a chat completion event to the Usage project.

    Args:
        user_id: The ID of the user
        model: The model used for the completion
        provider: The provider of the model
        request_body: The request body sent to the model
        response_body: The response received from the model
        usage: Usage statistics for the completion
        timestamp: The timestamp of the completion

    Returns:
        List of created log event IDs
    """
    try:
        # Initialize DAOs
        organization_member_dao = OrganizationMemberDAO(session=session)
        context_dao = ContextDAO(session=session)
        project_dao = ProjectDAO(
            session=session,
            organization_member_dao=organization_member_dao,
            context_dao=context_dao,
        )
        field_type_dao = FieldTypeDAO(session=session)
        log_event_dao = LogEventDAO(session=session)
        log_dao = LogDAO(session=session, context_dao=context_dao)

        # Get or create the Usage project
        project = get_or_create_usage_project(project_dao, user_id)

        # Create the log config
        at = datetime.now(timezone.utc)
        config = CreateLogConfig(
            project=settings.chat_completions_project_name,
            entries={**kwargs, "user_id": user_id, "at": at.isoformat()},
            params={},
        )
        context_id = context_dao.get_or_create(
            project.id,
            name="",
        )
        context_obj = session.get(Context, context_id)
        # Create the logs
        log_event_ids = create_logs_internal(
            request=config,
            project_id=project.id,
            context_id=context_id,
            context_obj=context_obj,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            log_event_dao=log_event_dao,
            log_dao=log_dao,
            context_dao=context_dao,
        )

        # Commit the session
        session.commit()

        return log_event_ids
    except Exception as e:
        session.rollback()
    finally:
        session.close()


def _build_unified_logs_limited(
    session,
    ids_subq: Subquery,
) -> Subquery:
    """
    Phase 2 helper: build unified logs subquery limited to the specified log_event_ids.
    """
    # IMPORTANT: pass the ID list through the *event_ids* parameter so the
    #            builder emits a simple "…WHERE LogEvent.id IN ( … )" filter
    #            that PostgreSQL can satisfy with the existing
    #            (log_event_id) b‑tree indexes instead of a hash‑join.
    # Pass only the single‑column list of IDs, not the whole CTE.
    id_only_sq = select(ids_subq.c.id).subquery("page_ids")
    return _build_unified_logs_subquery(
        session=session,
        event_ids=id_only_sq,
    )


def _build_sort_criteria(
    val_col: ColumnClause,
    sort_key: str,
    field_types: Dict[str, str],
):
    # If recognized type => cast
    if sort_key in field_types:
        pytype = field_types[sort_key]
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

        # This optimization only applies when sorting by a single vector similarity metric.
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


def _prefetch_json_values(session, paginated_ids_subq):
    """
    Return two sub‑queries with the current JSONLog value and the
    latest JSONLogHistory value for every (event_id,key) in the page.
    Uses DISTINCT ON, so it is index‑only and executed once.
    """
    jl_vals = (
        select(
            LogEventJSONLog.log_event_id,
            JSONLog.key,
            JSONLog.value.label("jl_val"),
        )
        .select_from(JSONLog)
        .join(
            LogEventJSONLog,
            LogEventJSONLog.json_log_id == JSONLog.id,
        )
        .where(LogEventJSONLog.log_event_id.in_(select(paginated_ids_subq.c.id)))
        .cte("jl_vals")
    )

    jlh_vals = (
        select(
            LogEventJSONLogHistory.log_event_id,
            JSONLogHistory.key,
            JSONLogHistory.value.label("jlh_val"),
        )
        .select_from(JSONLogHistory)
        .join(
            LogEventJSONLogHistory,
            LogEventJSONLogHistory.json_log_history_id == JSONLogHistory.id,
        )
        .where(LogEventJSONLogHistory.log_event_id.in_(select(paginated_ids_subq.c.id)))
        .distinct(LogEventJSONLogHistory.log_event_id, JSONLogHistory.key)
        .order_by(
            LogEventJSONLogHistory.log_event_id,
            JSONLogHistory.key,
            JSONLogHistory.version.desc(),
        )
        .cte("jlh_vals")
    )
    return jl_vals, jlh_vals


def _get_logs_query(
    request_fastapi: Request,
    project: str,
    column_context: Optional[str],
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
    latest_timestamp=False,
    randomize: bool = False,
    seed: Optional[str] = "42",
):
    """
    Returns a combined list of base logs (Log) and derived logs (DerivedLog)
    that match the given user filters. See docstring above for details.
    """
    user_id = request_fastapi.state.user_id

    # 1) Validate the project
    try:
        project_id = project_dao.get_by_user_and_name(name=project, user_id=user_id).id
    except (IndexError, AttributeError):
        raise not_found(f"Project {project}")

    # Phase 1: filtering, sorting, pagination, etc.
    log_event_query = session.query(LogEvent.id).filter(
        LogEvent.project_id == project_id,
    )
    context_name = "" if not context else context
    context_obj = context_dao.filter(name=context_name, project_id=project_id)
    if context_obj:
        context_id = context_obj[0][0].id
        log_event_query = log_event_query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )
    else:
        context_id = None

    field_types = field_type_dao.get_field_types(project_id, context_id=context_id)

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

            # Define a subquery for event IDs to pass to the query builder
            event_ids_subq = log_event_query.subquery(name="event_ids_subq")

            try:
                # --- OPTIMIZATION FOR 'OR' ---
                if isinstance(filter_dict, dict) and filter_dict.get("operand") == "or":
                    or_conditions = flatten_or_conditions(filter_dict)
                    matching_id_subqueries = []

                    for i, condition_dict in enumerate(or_conditions):
                        validate_filter_dict(condition_dict)
                        condition_sql = build_sql_query(
                            condition_dict,
                            LogEvent,
                            session,
                            log_event_ids=event_ids_subq,
                        )
                        if isinstance(condition_sql, Subquery):
                            truthiness_clause = _create_truthiness_condition(
                                condition_sql,
                                session,
                            )
                            matching_ids = select(condition_sql.c.log_event_id).where(
                                truthiness_clause,
                            )
                            matching_id_subqueries.append(matching_ids)

                    if matching_id_subqueries:
                        unioned_ids_subq = union_all(*matching_id_subqueries).subquery()
                        log_event_query = log_event_query.filter(
                            LogEvent.id.in_(select(unioned_ids_subq)),
                        )

                # --- OPTIMIZATION FOR 'AND' ---
                elif (
                    isinstance(filter_dict, dict)
                    and filter_dict.get("operand") == "and"
                ):
                    and_conditions = flatten_and_conditions(filter_dict)

                    for i, condition_dict in enumerate(and_conditions):
                        validate_filter_dict(condition_dict)
                        condition_sql = build_sql_query(
                            condition_dict,
                            LogEvent,
                            session,
                            log_event_ids=event_ids_subq,
                        )
                        if isinstance(condition_sql, Subquery):
                            truthiness_clause = _create_truthiness_condition(
                                condition_sql,
                                session,
                            )
                            log_event_query = log_event_query.filter(
                                exists(
                                    select(1)
                                    .select_from(condition_sql)
                                    .where(
                                        and_(
                                            condition_sql.c.log_event_id == LogEvent.id,
                                            truthiness_clause,
                                        ),
                                    ),
                                ),
                            )
                        else:
                            log_event_query = log_event_query.filter(condition_sql)

                # --- FALLBACK FOR SINGLE CONDITIONS OR OTHER OPERATORS ---
                else:
                    validate_filter_dict(filter_dict)
                    condition_sql = build_sql_query(
                        filter_dict,
                        LogEvent,
                        session,
                        log_event_ids=event_ids_subq,
                    )
                    if isinstance(condition_sql, Subquery):
                        truthiness_clause = _create_truthiness_condition(
                            condition_sql,
                            session,
                        )
                        log_event_query = log_event_query.filter(
                            exists(
                                select(1)
                                .select_from(condition_sql)
                                .where(
                                    and_(
                                        condition_sql.c.log_event_id == LogEvent.id,
                                        truthiness_clause,
                                    ),
                                ),
                            ),
                        )
                    else:
                        log_event_query = log_event_query.filter(condition_sql)

            except Exception as e:
                session.rollback()
                # Provide detailed error information
                error_msg = f"Error processing filter expression: {str(e)}"
                if hasattr(e, "__class__"):
                    error_msg = f"{e.__class__.__name__}: {error_msg}"
                raise HTTPException(
                    status_code=400,
                    detail=error_msg,
                )

    # Apply from_ids/exclude_ids filters early since they filter on log_event_id
    if from_ids and exclude_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )

    if from_ids:
        include_ids = [int(x) for x in from_ids.split("&")]
        log_event_query = log_event_query.filter(
            LogEvent.id.in_(include_ids),
        )
    elif exclude_ids:
        exclude_set = [int(x) for x in exclude_ids.split("&")]
        log_event_query = log_event_query.filter(
            LogEvent.id.notin_(exclude_set),
        )

    # Apply field filters at log event level
    if from_fields and exclude_fields:
        raise HTTPException(
            status_code=400,
            detail="Only one of from_fields or exclude_fields can be set.",
        )

    if from_fields:
        # Filter to only include log events that have at least one of the specified fields
        allowed_fields = from_fields.split("&")
        # Check both Log and DerivedLog tables for matching fields
        log_exists = (
            session.query(LogEventLog.log_event_id)
            .join(Log, LogEventLog.log_id == Log.id)
            .filter(
                LogEventLog.log_event_id == LogEvent.id,
                Log.key.in_(allowed_fields),
            )
            .exists()
        )
        derived_log_exists = (
            session.query(LogEventDerivedLog.log_event_id)
            .join(DerivedLog, DerivedLog.id == LogEventDerivedLog.derived_log_id)
            .filter(
                LogEventDerivedLog.log_event_id == LogEvent.id,
                DerivedLog.key.in_(allowed_fields),
            )
            .exists()
        )
        log_event_query = log_event_query.filter(
            or_(log_exists, derived_log_exists),
        )
    elif exclude_fields:
        # Filter to only include log events that have at least one field NOT in the excluded list
        excluded_fields = exclude_fields.split("&")
        # Check both Log and DerivedLog tables for non-excluded fields
        log_exists = (
            session.query(LogEventLog.log_event_id)
            .join(Log, LogEventLog.log_id == Log.id)
            .filter(
                LogEventLog.log_event_id == LogEvent.id,
                Log.key.notin_(excluded_fields),
            )
            .exists()
        )
        derived_log_exists = (
            session.query(LogEventDerivedLog.log_event_id)
            .join(DerivedLog, DerivedLog.id == LogEventDerivedLog.derived_log_id)
            .filter(
                LogEventDerivedLog.log_event_id == LogEvent.id,
                DerivedLog.key.notin_(excluded_fields),
            )
            .exists()
        )
        log_event_query = log_event_query.filter(
            or_(log_exists, derived_log_exists),
        )

    # FIXME: potential duplicate logic
    if context:
        context_obj = context_dao.filter(name=context, project_id=project_id)
    else:
        context_obj = context_dao.filter(name="", project_id=project_id)
        if not context_obj:
            if latest_timestamp:
                project_obj = project_dao.filter(name=project, user_id=user_id)
                return project_obj[0][0].created_at.isoformat()
            else:
                return [], 0, 0

    if not context_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context}' not found",
        )
    context_obj = context_obj[0][0]
    ctx_id_val = context_obj.id

    # ---- Phase-1: gather all event IDs that match user filters ------------
    # Note: filter_expr has already been applied to log_event_query

    # Build ORDER BY expressions
    sort_val_sqs: List[Subquery] = []
    sort_criteria: List[Any] = []

    if not randomize and sorting:
        # Step 1: Create relevant log events subquery
        relevant_log_events_cte = log_event_query.cte(
            "relevant_log_events",
        ).prefix_with(  # WITH relevant_log_events AS ( … )
            "MATERIALIZED",
        )  # force single evaluation in PG ≥12
        # keep a handy alias – this line replaces every previous reference
        relevant_log_events = relevant_log_events_cte

        # Step 2: Build sort clauses
        is_vector_sort, vector_sort_details = _build_sort_clauses(
            session,
            log_event_query,
            field_types,
            sorting,
            relevant_log_events,
            sort_val_sqs,
            sort_criteria,
        )

        if is_vector_sort:
            # --- vector ANN fast-path  ---

            # 0) Use the already-built CTE of filtered IDs
            event_ids = relevant_log_events  # CTE with column "id"

            # 1) Extract parsed pieces from vector_sort_details
            expr_dict = vector_sort_details["expr_dict"]
            operand = vector_sort_details["operand"]  # "cosine" | "l2" | "ip"
            mode = vector_sort_details["mode"]  # "ascending" | "descending"
            lhs_key = vector_sort_details["lhs_key"]  # e.g. "_content_text_emb"
            rhs_embed = vector_sort_details[
                "rhs_embed"
            ]  # parsed embed(...) dict or expr

            # 2) Build RHS vector literal once
            rhs_sql = build_sql_query(
                rhs_embed,
                LogEvent,
                session,
                log_event_ids=event_ids,  # scope for any correlated pieces (should be literal)
            )
            rhs_vec, _ = _select_value(rhs_sql, session, is_vector=True)

            # 3) Detect the model and dimension for this key
            # Query to get the model used for this embedding key
            embedding_model_query = session.execute(
                select(Embedding.model).where(Embedding.key == lhs_key).limit(1),
            ).scalar()

            # Map model to dimension for proper casting
            model_to_dim = {
                "text-embedding-3-small": 1536,
                "multimodalembedding@001": 1408,
            }
            embedding_dim = model_to_dim.get(embedding_model_query, None)

            # 4) Choose the correct distance operator and cast vector
            op = {"cosine": "<=>", "l2": "<->", "ip": "<#>"}[operand]

            # Cast the vector to the correct dimension to use the HNSW index
            # This is critical for pgvector to use the model-specific partial indexes
            if embedding_dim and embedding_model_query:
                # Use casted vector to match the expression index
                casted_vector = func.cast(Embedding.vector, Vector(embedding_dim))
                dist = casted_vector.op(op)(rhs_vec)
                # Add model filter for the partial index
                model_filter = Embedding.model == embedding_model_query
            else:
                # Fallback: no cast (will be slower without index)
                dist = Embedding.vector.op(op)(rhs_vec)
                model_filter = literal(True)

            asc_sort = mode == "ascending"

            # 5) ANN top-K on Embedding first (pushdown LIMIT)
            top_k = (offset or 0) + (limit or 100)
            ann_topk = (
                select(
                    Embedding.ref_id.label("id"),
                    dist.label("dist"),
                )
                .where(
                    Embedding.key == lhs_key,
                    model_filter,  # Filter by model for partial index
                    Embedding.vector.isnot(None),
                    Embedding.ref_id.in_(select(event_ids.c.id)),
                )
                .order_by(
                    dist.asc() if asc_sort else dist.desc(),
                    Embedding.ref_id.desc(),
                )
                .limit(top_k)
                .subquery("ann_topk")
            )

            # 5) Page and expose row numbers for downstream logic
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

            paginated_ids_subq = paginated_ids_subq.cte("paginated_ids").prefix_with(
                "MATERIALIZED",
            )

            # Keep total_count consistent with legacy path
            total_count = log_event_query.distinct().count()

        else:
            # Step 3: Add deterministic tie-breaker
            sort_criteria.append(desc(relevant_log_events.c.id))

            # Step 4: Join sort subqueries with log events
            joined_events = relevant_log_events
            for i, sq in enumerate(sort_val_sqs):
                joined_events = joined_events.outerjoin(
                    sq,
                    sq.c.log_event_id == relevant_log_events.c.id,
                )

            # Step 5: Build final query with sort info
            base_event_q = session.query(relevant_log_events.c.id).select_from(
                joined_events,
            )

            # For _paginate_events, we need to pass the joined query and sort criteria
            # This will ensure proper ordering without cartesian products
            has_joins = True

            # Calculate total count using the query without any joins or sorting
            total_count = log_event_query.distinct().count()

            # Paginate the events
            paginated_ids_subq = _paginate_events(
                session,
                base_event_q,
                sort_criteria,
                limit,
                offset,
                randomize=randomize,
                seed=seed,
                has_joins=has_joins,
            )
            paginated_ids_cte = (
                select(paginated_ids_subq.c.id, paginated_ids_subq.c.row_num)  # ⬅ wrap
                .cte("paginated_ids")  #     then cte()
                .prefix_with("MATERIALIZED")  # (PG ≥12)
            )
            paginated_ids_subq = paginated_ids_cte

    else:
        # No sorting needed, just use the filtered events
        base_event_q = log_event_query

        # ---- Phase-2: total_count + page -------------------------------
        # Check if we have joins (when sorting is enabled)
        has_joins = bool(sorting) and not randomize

        # Calculate total count using the query without any joins or sorting
        total_count = log_event_query.distinct().count()

        # Paginate the events
        paginated_ids_subq = _paginate_events(
            session,
            base_event_q,
            sort_criteria,
            limit,
            offset,
            randomize=randomize,
            seed=seed,
            has_joins=has_joins,
        )
        paginated_ids_cte = (
            select(paginated_ids_subq.c.id, paginated_ids_subq.c.row_num)  # ⬅ wrap
            .cte("paginated_ids")  #     then cte()
            .prefix_with("MATERIALIZED")  # (PG ≥12)
        )
        paginated_ids_subq = paginated_ids_cte

    # Phase 3: Handle special cases
    if latest_timestamp:
        # Build unified logs only for timestamp check
        unified_logs_for_timestamp = _build_unified_logs_subquery(
            session=session,
            relevant_log_events=paginated_ids_subq,
        )
        max_updated_at = session.query(
            func.max(unified_logs_for_timestamp.c.updated_at),
        ).scalar()
        result = max_updated_at.isoformat() if max_updated_at else None
        return result

    # ---- Phase-4: build unified logs ONLY for the paginated IDs ----
    unified_logs_limited = _build_unified_logs_limited(
        session,
        paginated_ids_subq,
    )

    filtered_logs_q = session.query(unified_logs_limited).filter(True)

    context_len = 0
    exclude_params = False
    exclude_entries = False
    if column_context is not None:
        split_context = column_context.split("/")
        exclude_params = "entries" in split_context
        exclude_entries = "params" in split_context
        if exclude_params and exclude_entries:
            raise HTTPException(
                status_code=400,
                detail="'entries' and 'params' cannot both be specified in column_context.",
            )
        column_context = "/".join(
            [substr for substr in split_context if substr not in ("params", "entries")],
        )
        if column_context and column_context[-1] != "/":
            column_context += "/"
        context_len = len(column_context or "")
    if column_context:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_limited.c.key.startswith(column_context),
        )

    filtered_logs_q = _apply_post_filters(
        filtered_logs_q,
        unified_logs_limited,
        from_ids=None,  # Already applied to log_event_query
        exclude_ids=None,  # Already applied to log_event_query
        exclude_params=exclude_params,
        exclude_entries=exclude_entries,
        from_fields=from_fields,  # Still need to filter the actual fields returned
        exclude_fields=exclude_fields,  # Still need to filter the actual fields returned
    )
    filtered_logs_subq = filtered_logs_q.subquery(name="filtered_logs_subq")

    # Get final logs - total_count already calculated in _paginate_events
    raw_rows = _get_final_logs(session, filtered_logs_subq, paginated_ids_subq)

    results = []
    for (
        row_id,
        row_event_id,
        row_key,
        row_value,
        row_inferred_type,
        row_param_version,
        row_context_version,
        row_created_at,
        row_source_type,
    ) in raw_rows:
        results.append(
            (
                row_key,
                row_value,
                row_inferred_type,
                row_param_version,
                row_context_version,
                row_source_type,
                row_created_at,
                row_event_id,
            ),
        )

    return results, context_len, total_count


def create_logs_internal(
    request: CreateLogConfig,
    project_id: int,
    context_id: int,
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    log_event_dao: LogEventDAO,
    log_dao: LogDAO,
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
        log_dao: Data access object for logs
        context_dao: Data access object for contexts

    Returns:
        List of created log event IDs

    Raises:
        HTTPException: If validation fails or duplicate logs are detected
    """
    # Convert single entries/params to list format for uniform processing
    entries_list = (
        request.entries if isinstance(request.entries, list) else [request.entries]
    )
    params_list = (
        request.params if isinstance(request.params, list) else [request.params]
    )

    # Validate and normalize params and entries
    if isinstance(request.entries, list) and isinstance(request.params, list):
        # Case 1: Both are lists - they should have equal lengths
        if len(request.entries) != len(request.params):
            raise HTTPException(
                status_code=400,
                detail=f"When both 'params' and 'entries' are provided as lists, they must have equal lengths. "
                f"Got params length: {len(request.params)}, entries length: {len(request.entries)}",
            )
    elif isinstance(request.entries, list) and (
        request.params is None or request.params == {}
    ):
        # Case 2: Entries is a list, params is None/empty - this is allowed
        params_list = [{}] * len(request.entries)
    elif isinstance(request.params, list) and (
        request.entries is None or request.entries == {}
    ):
        # Case 2: Params is a list, entries is None/empty - this is allowed
        entries_list = [{}] * len(request.params)
    elif isinstance(request.entries, list) and isinstance(request.params, dict):
        # Case 3: Entries is a list, params is a dict - convert params to a list of the same dict
        params_list = [
            {k: v for k, v in request.params.items()}
            for _ in range(len(request.entries))
        ]
    elif isinstance(request.params, list) and isinstance(request.entries, dict):
        # Case 3: Params is a list, entries is a dict - convert entries to a list of the same dict
        entries_list = [
            {k: v for k, v in request.entries.items()}
            for _ in range(len(request.params))
        ]

    # Get field types once for all operations
    field_types = field_type_dao.get_field_types(
        project_id,
        return_mutable=True,
        context_id=context_id,
    )

    def enforce_types(
        field_name,
        value,
        batch_index=None,
        explicit_types=None,
        context_id=None,
        is_param=False,
    ):
        entered_type = LogDAO.infer_type(field_name, value)
        field_info = field_types.get(field_name)
        if field_info:
            # Check field category first
            existing_category = field_info["field_category"]
            new_category = "param" if is_param else "entry"
            if existing_category != new_category:
                new_article = "an" if new_category == "entry" else "a"
                existing_article = "an" if existing_category == "entry" else "a"
                raise HTTPException(
                    status_code=400,
                    detail=f"Field '{field_name}' already exists as {existing_article} {existing_category}. Cannot create it as {new_article} {new_category}.",
                )

        # Then check data type
        expected_type = field_info["field_type"] if field_info else None
        if expected_type:
            if expected_type == "NoneType":
                if entered_type == "NoneType":
                    return
                # update the field type to the new type
                field_type_dao.upsert_field_type(
                    project_id,
                    field_name,
                    value,
                    mutable=field_info.get("mutable", False),
                    unique=field_info.get("unique", False),
                    field_category="param" if is_param else "entry",
                    context_id=context_id,
                )
            elif entered_type != expected_type and entered_type != "NoneType":
                batch_info = (
                    f" (in batch entry {batch_index})"
                    if batch_index is not None
                    else ""
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Type mismatch for field '{field_name}'{batch_info}: expected {expected_type}, got {entered_type}. Value: {str(value)[:100]}",
                )
        else:
            # Extract mutable and unique flags from explicit_types if present
            mutable = (
                explicit_types.get(field_name, {}).get("mutable", False)
                if explicit_types
                else False
            )
            unique = (
                explicit_types.get(field_name, {}).get("unique", False)
                if explicit_types
                else False
            )
            # If in a versioned context, force mutable=True
            if context_id and context_dao.is_versioned(context_id):
                mutable = True
            field_type_dao.create_field_type_if_absent(
                project_id,
                field_name,
                value,
                mutable=mutable,
                unique=unique,
                field_category="param" if is_param else "entry",
                context_id=context_id,
            )

    # Bulk create all log events at once
    entries_len = len(entries_list)
    params_len = len(params_list)
    total_logs = max(entries_len, params_len)

    provided_unique_ids = None
    # Handle auto-counting fields (both in unique_keys and not)
    if context_obj and (context_obj.unique_keys or context_obj.auto_counting):
        unique_keys = context_obj.unique_keys or {}
        auto_counting = context_obj.auto_counting or {}

        # 1. Extract and validate composite key values from entries/params
        all_composite_values = []
        for i in range(total_logs):
            current_entries = entries_list[min(i, len(entries_list) - 1)] or {}
            current_params = params_list[min(i, len(params_list) - 1)] or {}

            # Merge entries and params to check for unique key columns
            current_data = {**current_entries, **current_params}

            # Extract values for composite key columns
            composite_values = {}
            provided_counting_values = {}

            # First process unique key columns
            for col_name, col_type in unique_keys.items():
                if col_name in auto_counting:
                    # For auto-counting columns, check if user provided a value
                    if col_name in current_data:
                        provided_counting_values[col_name] = current_data[col_name]
                else:
                    # Non-auto-counting columns must be provided
                    if col_name not in current_data:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Must provide value for composite key column '{col_name}' (type: {col_type}).",
                        )
                    composite_values[col_name] = current_data[col_name]

            # Then process auto-counting columns that are NOT in unique keys
            for col_name, parent_col in auto_counting.items():
                if col_name not in unique_keys and col_name in current_data:
                    provided_counting_values[col_name] = current_data[col_name]

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

        # 2. Pop composite key columns from original entries/params to prevent them from becoming log fields
        for i in range(total_logs):
            current_entries = entries_list[min(i, len(entries_list) - 1)]
            current_params = params_list[min(i, len(params_list) - 1)]
            composite_values = all_composite_values[i]

            # Remove composite key columns from entries and params
            if current_entries:
                for key in unique_keys.keys():
                    current_entries.pop(key, None)
            if current_params:
                for key in unique_keys.keys():
                    current_params.pop(key, None)

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
    log_records_to_create = []

    # Process all logs in the batch
    for i in range(total_logs):
        log_event_id = log_event_ids[i]

        # Get current entries and params
        # If i exceeds list length, use the last item in the list
        current_entries = entries_list[min(i, entries_len - 1)]
        current_params = params_list[min(i, params_len - 1)]

        # Add auto-incremented values from row_ids that are not in unique_keys back to entries
        if context_obj and context_obj.auto_counting and row_ids and i < len(row_ids):
            row_id_dict = row_ids[i] if isinstance(row_ids[i], dict) else {}
            unique_keys = context_obj.unique_keys or {}

            for col_name, col_value in row_id_dict.items():
                # Only add if it's an auto-counting field that's NOT in unique_keys
                # (unique_key fields are already handled by log_event_dao.bulk_create)
                if (
                    col_name in context_obj.auto_counting
                    and col_name not in unique_keys
                ):
                    if (
                        col_name not in current_entries
                        and col_name not in current_params
                    ):
                        # Add to entries
                        current_entries[col_name] = col_value

        # Extract explicit types - NOTE: This mutates entries/params dicts in-place
        # Callers should pass fresh copies if they need to reuse the original dicts
        entries_explicit_types = (
            current_entries.pop("explicit_types", {})
            if isinstance(current_entries, dict)
            else None
        )
        params_explicit_types = (
            current_params.pop("explicit_types", {})
            if isinstance(current_params, dict)
            else None
        )

        # Process params - collect them for bulk creation
        for k, v in current_params.items():
            # Check and register new field types if needed
            if k not in field_types:
                mutable = (
                    params_explicit_types.get(k, {}).get("mutable", False)
                    if params_explicit_types
                    else False
                )
                unique = (
                    params_explicit_types.get(k, {}).get("unique", False)
                    if params_explicit_types
                    else False
                )
                # If in a versioned context, force mutable=True
                if context_obj and context_obj.is_versioned:
                    mutable = True
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": k,
                        "value": v,
                        "mutable": mutable,
                        "unique": unique,
                        "field_category": "param",
                        "context_id": context_id,
                    },
                )
            else:
                # Enforce types for existing fields
                enforce_types(k, v, i, params_explicit_types, context_id, is_param=True)

            # Determine version for parameter
            existing_param = log_dao.filter(
                key=k,
                value=json.dumps(v),
                project_id=project_id,
            )
            if existing_param:
                version = existing_param[0][0].param_version
            else:
                version = log_dao.get_next_param_version(project_id, context_id, k)

            # Add to records for bulk creation
            log_records_to_create.append(
                {
                    "project_id": project_id,
                    "log_event_id": log_event_id,
                    "key": k,
                    "value": v,
                    "param_version": version,
                    "explicit_types": params_explicit_types,
                    "context_id": context_id,
                },
            )

        # Process entries - collect them for bulk creation
        for k, v in current_entries.items():
            # Check and register new field types if needed
            if k not in field_types:
                mutable = (
                    entries_explicit_types.get(k, {}).get("mutable", False)
                    if entries_explicit_types
                    else False
                )
                unique = (
                    entries_explicit_types.get(k, {}).get("unique", False)
                    if entries_explicit_types
                    else False
                )
                # If in a versioned context, force mutable=True
                if context_obj and context_obj.is_versioned:
                    mutable = True
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": k,
                        "value": v,
                        "mutable": mutable,
                        "unique": unique,
                        "field_category": "entry",
                        "context_id": context_id,
                    },
                )
            else:
                # Enforce types for existing fields
                enforce_types(
                    k,
                    v,
                    i,
                    entries_explicit_types,
                    context_id,
                    is_param=False,
                )

            # Add to records for bulk creation (entries don't have version)
            log_records_to_create.append(
                {
                    "project_id": project_id,
                    "log_event_id": log_event_id,
                    "key": k,
                    "value": v,
                    "explicit_types": entries_explicit_types,
                    "context_id": context_id,
                },
            )

    # Bulk create new field types if any
    try:
        if new_field_types:
            field_type_dao.bulk_create_field_types(new_field_types)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Bulk create all log records
    try:
        log_dao.bulk_create(log_records_to_create, context_obj=context_obj)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Check for duplicates if context doesn't allow duplicates
    if context_obj and not context_obj.allow_duplicates:
        for log_event_id in log_event_ids:
            # Check for duplicates
            duplicate = context_dao.check_for_duplicates(context_obj.id, log_event_id)
            if duplicate:
                log_event_dao.delete(log_event_id)
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate log detected in context '{context_obj.name}' which doesn't allow duplicates. Log event ID: {log_event_id}",
                )
    if context_obj and context_obj.is_versioned:
        context_obj.updated_at = datetime.now(timezone.utc)

    # Build row_ids payload (unique key columns only)
    row_ids_payload = None
    unique_keys = context_obj.unique_keys or {}

    names = []
    ids_list = []

    # Transform row_ids into the list format
    if row_ids and unique_keys:
        # Use the preserved order from context
        names = context_obj.unique_key_names or list(unique_keys.keys())

        if isinstance(row_ids[0], dict):
            # Dictionary format - convert to list of lists using the correct order
            for row_id in row_ids:
                ids_list.append([row_id.get(name) for name in names])
        else:
            # Legacy single ID case - wrap each ID in a list
            ids_list = [[row_id] for row_id in row_ids]

    row_ids_payload = {
        "names": names,
        "ids": ids_list,
    }

    # Build auto_counting payload (all auto-counting columns with their values)
    # row_ids from DAO contains dictionaries with ALL auto_counting columns (both in unique_keys and not)
    # Always return a dict, empty if no auto-counting configured
    auto_counting_payload = {}
    auto_counting_cfg = context_obj.auto_counting or {}
    if row_ids and auto_counting_cfg and isinstance(row_ids[0], dict):
        # Extract auto-counting values as a dict mapping column name to list of values
        for col_name in auto_counting_cfg.keys():
            auto_counting_payload[col_name] = [
                row_id.get(col_name) for row_id in row_ids
            ]

    return {
        "log_event_ids": log_event_ids,
        "row_ids": row_ids_payload,
        "auto_counting": auto_counting_payload,
    }


# TODO(yusha): refactor get_logs_query to make it modular
def _build_unified_logs_subquery(
    session,
    event_ids: Optional[Subquery] = None,
    relevant_log_events: Optional[Subquery] = None,
    key: str = None,
) -> Subquery:
    """
    Build a unified subquery that combines base logs and derived logs.

    Args:
        session: The database session
        event_ids: Optional list of event IDs to filter by directly
        relevant_log_events: Optional subquery containing relevant log event IDs to join with

    Returns:
        A unified subquery combining base and derived logs
    """
    if event_ids is None and relevant_log_events is None:
        raise ValueError("Either event_ids or relevant_log_events must be provided")

    def _apply_event_filter(query, table):
        if event_ids is not None:
            # if we were given a Subquery alias, wrap it in a scalar SELECT
            event_ids_selectable = (
                select(event_ids) if isinstance(event_ids, Subquery) else event_ids
            )
            return query.filter(LogEvent.id.in_(event_ids_selectable))
        query = query.join(relevant_log_events, relevant_log_events.c.id == LogEvent.id)
        if hasattr(relevant_log_events.c, "row_num"):
            query = query.order_by(relevant_log_events.c.row_num)
        if key:
            # Filter down to only logs with the specified key (for performance)
            query = query.filter(table.key == key)
        return query

    # get only the latest version of the logs
    base_logs_q = (
        session.query(
            Log.id.label("id"),
            LogEventLog.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.param_version.label("param_version"),
            cast(None, Integer).label("context_version"),
            Log.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("base").label("source_type"),
        )
        .join(LogEventLog, LogEventLog.log_id == Log.id)
        .join(LogEvent, LogEvent.id == LogEventLog.log_event_id)
    )
    base_logs_q = _apply_event_filter(base_logs_q, Log)

    derived_logs_q = (
        session.query(
            DerivedLog.id.label("id"),
            LogEventDerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.key.label("key"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
            # derived logs have no version => cast to None
            cast(None, Integer).label("param_version"),
            cast(None, Integer).label("context_version"),
            DerivedLog.updated_at.label("updated_at"),
            DerivedLog.created_at.label("created_at"),
            literal("derived").label("source_type"),
        )
        .join(LogEventDerivedLog, LogEventDerivedLog.derived_log_id == DerivedLog.id)
        .join(LogEvent, LogEvent.id == LogEventDerivedLog.log_event_id)
    )
    derived_logs_q = _apply_event_filter(derived_logs_q, DerivedLog)

    unified_logs_subq = base_logs_q.union_all(derived_logs_q).subquery(
        name="unified_logs",
    )
    # re-label columns to avoid anonymous column names
    result = select(
        unified_logs_subq.c[unified_logs_subq.c.keys()[0]].label("id"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[1]].label("log_event_id"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[2]].label("key"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[3]].label("value"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[4]].label("inferred_type"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[5]].label("param_version"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[6]].label("context_version"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[7]].label("updated_at"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[8]].label("created_at"),
        unified_logs_subq.c[unified_logs_subq.c.keys()[9]].label("source_type"),
    ).subquery("unified_logs")

    return result


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

        # Apply context_len slicing to the key
        key = row_key

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
    params_out = {}
    for event_id, data in formatted.items():
        entries = {}
        params = {}
        for k, v in data["entries"].items():
            if k in data["versions"]:
                # It's param-based
                params[k] = v  # v is the str(ver)
                # Also store in params_out if needed
                if k not in params_out:
                    params_out[k] = {}
                # We might have multiple versions for the same param
                for ver_num, ver_val in data["versions"][k].items():
                    params_out[k][ver_num] = ver_val
            else:
                # It's a normal base entry
                entries[k] = v

        # derived_entries
        derived_entries = data["derived_entries"]

        # Sort all dictionaries according to field_type order
        sorted_entries = dict(
            sorted(
                entries.items(),
                key=lambda x: field_order_map.get(x[0], float("inf")),
            ),
        )
        sorted_params = dict(
            sorted(
                params.items(),
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
                "params": sorted_params,
                "derived_entries": sorted_derived,
                "versions": sorted_context_versions,
                "clipped_fields": data.get("clipped_fields", []),
            },
        )

    return logs_out, params_out


def _get_final_logs(session, filtered_logs_subq, paginated_ids_subq):
    """
    Return fully-hydrated rows, using LATERAL sub-queries so the JSON
    side-tables are probed with indexes instead of being full-scanned.
    """
    # ── current JSON value and latest history value ────────────────────────────────────────────────
    jl_vals, jlh_vals = _prefetch_json_values(session, paginated_ids_subq)

    # -- Main query --------------------------------------------------------------
    final_logs_query = (
        session.query(
            filtered_logs_subq.c.id,
            filtered_logs_subq.c.log_event_id,
            filtered_logs_subq.c.key,
            func.coalesce(
                case(
                    (
                        filtered_logs_subq.c.source_type == "history",
                        jlh_vals.c.jlh_val,
                    ),
                    else_=jl_vals.c.jl_val,
                ),
                cast(filtered_logs_subq.c.value, JSON),
            ).label("value"),
            filtered_logs_subq.c.inferred_type,
            filtered_logs_subq.c.param_version,
            filtered_logs_subq.c.context_version,
            filtered_logs_subq.c.created_at,
            filtered_logs_subq.c.source_type,
        )
        # keep page order information first
        .join(
            paginated_ids_subq,
            paginated_ids_subq.c.id == filtered_logs_subq.c.log_event_id,
        )
        # probe the side tables (set joins)
        .outerjoin(
            jl_vals,
            and_(
                jl_vals.c.log_event_id == filtered_logs_subq.c.log_event_id,
                jl_vals.c.key == filtered_logs_subq.c.key,
            ),
        )
        .outerjoin(
            jlh_vals,
            and_(
                jlh_vals.c.log_event_id == filtered_logs_subq.c.log_event_id,
                jlh_vals.c.key == filtered_logs_subq.c.key,
            ),
        )
        .order_by(paginated_ids_subq.c.row_num, filtered_logs_subq.c.created_at)
    )

    # Execute the query
    result = final_logs_query.all()
    return result


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
    Helper function to build a SQLAlchemy subquery from log filtering criteria.

    Args:
        args: Dictionary containing filtering criteria
        project_name: Name of the project
        request_fastapi: FastAPI request object
        project_dao: ProjectDAO instance
        field_type_dao: FieldTypeDAO instance
        context_dao: ContextDAO instance
        session: SQLAlchemy session
        alias: Alias name for the subquery

    Returns:
        SQLAlchemy subquery object
    """
    # Import the necessary function from views.py to build subqueries
    from orchestra.web.api.log.views import _get_all_filtered_log_event_ids

    # Extract filtering criteria from args
    column_context = args.get("column_context")
    context = args.get("context")
    filter_expr = args.get("filter_expr")
    from_ids = args.get("from_ids")
    exclude_ids = args.get("exclude_ids")

    # Get filtered log event IDs as a subquery
    event_ids_subq, _ = _get_all_filtered_log_event_ids(
        request_fastapi=request_fastapi,
        project=project_name,
        context=context,
        filter_expr=filter_expr,
        from_ids=from_ids,
        exclude_ids=exclude_ids,
        project_dao=project_dao,
        context_dao=context_dao,
        field_type_dao=field_type_dao,
        session=session,
        as_subquery=True,  # Return as a subquery
    )

    # Get context ID for field type lookup
    context_id = None
    if context:
        context_id = context_dao.get_or_create(
            project_id,
            name=context,
        )

    # Start with a base query selecting log_event_id
    base_query = session.query(LogEvent.id.label("log_event_id"))

    # Try to get field names from FieldTypeDAO
    log_keys = []
    try:
        # Get ordered field names from FieldTypeDAO
        field_names_dict = field_type_dao.get_field_types(
            project_id=project_id,
            context_id=context_id,
        )
        if field_names_dict:
            # Convert to list and sort by the order index
            log_keys = [
                k
                for k, _ in sorted(
                    field_names_dict.items(),
                    key=lambda item: item[1],
                )
            ]
    except Exception as e:
        raise ValueError(f"Error getting field types: {str(e)}")

    # For each key, add a lateral subquery that gets its value from either Log or DerivedLog
    for key in log_keys:
        # Create a subquery that gets the value for this key from base logs
        base_log_subq = (
            session.query(Log.value)
            .join(LogEventLog, LogEventLog.log_id == Log.id)
            .filter(LogEventLog.log_event_id == LogEvent.id, Log.key == key)
            .limit(1)
            .scalar_subquery()
        )

        # Create scalar subqueries for derived log fields
        derived_log_subq = (
            session.query(DerivedLog.value)
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .filter(
                LogEventDerivedLog.log_event_id == LogEvent.id,
                DerivedLog.key == key,
            )
            .limit(1)
            .scalar_subquery()
        )

        # Create scalar subqueries for metadata
        derived_log_equation_subq = (
            session.query(DerivedLog.equation)
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .filter(
                LogEventDerivedLog.log_event_id == LogEvent.id,
                DerivedLog.key == key,
            )
            .limit(1)
            .scalar_subquery()
        )

        derived_log_referenced_logs_subq = (
            session.query(DerivedLog.referenced_logs)
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .filter(
                LogEventDerivedLog.log_event_id == LogEvent.id,
                DerivedLog.key == key,
            )
            .limit(1)
            .scalar_subquery()
        )

        # Use COALESCE to get the value from either base or derived logs (preferring base logs)
        key_subq = func.coalesce(base_log_subq, derived_log_subq).label(key)
        base_query = base_query.add_columns(key_subq)

        # Add metadata columns to track source with table prefixes
        source_col = case(
            (base_log_subq.isnot(None), literal("log")),
            (derived_log_subq.isnot(None), literal("derived_log")),
            else_=literal(None),
        ).label(f"{key}__orchestra__source")
        base_query = base_query.add_columns(source_col)

        # Add equation and referenced_logs for derived logs with table prefixes
        base_query = base_query.add_columns(
            derived_log_equation_subq.label(f"{key}__derived_log__equation"),
            derived_log_referenced_logs_subq.label(
                f"{key}__derived_log__referenced_logs",
            ),
        )

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
    Constructs a join query between two subqueries based on the specified join mode.

    Args:
        subq_a: First subquery (aliased as 'A')
        subq_b: Second subquery (aliased as 'B')
        join_expr: SQL expression for the join condition
        mode: Type of join ('inner', 'left', 'right', or 'outer')
        columns: Optional dictionary mapping source columns to new column names or list of source columns to include

    Returns:
        SQLAlchemy select statement representing the join
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

        # 2. Build the local_scope dictionary mapping placeholders to column objects
        local_scope = {"subq_a": subq_a, "subq_b": subq_b}
        for col in subq_a.c.keys():
            if col in fields_a:
                local_scope[f"__table_A_{col}"] = (getattr(subq_a.c, col), "column")
        for col in subq_b.c.keys():
            if col in fields_b:
                local_scope[f"__table_B_{col}"] = (getattr(subq_b.c, col), "column")

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
        select_columns.append(getattr(subq_a.c, "log_event_id").label("log_event_id_a"))
        select_columns.append(getattr(subq_b.c, "log_event_id").label("log_event_id_b"))

    if columns:
        # Convert columns to a unified format: list of (source_col, label) tuples
        if isinstance(columns, dict):
            column_specs = list(columns.items())
        elif isinstance(columns, list):
            # For list format, use the column name as the label
            column_specs = [(col, col.split(".", 1)[1]) for col in columns]
        else:
            raise ValueError("columns must be either a dictionary or a list")

        # Process all columns in a unified way
        for source_col, label in column_specs:
            if "." not in source_col:
                raise ValueError(
                    f"Column '{source_col}' must be prefixed with table alias 'A.' or 'B.'",
                )

            table_alias, actual_col = source_col.split(".", 1)
            table_alias = table_alias.upper()

            if table_alias == "A":
                subq = subq_a
                source_name = "source A"
            elif table_alias == "B":
                subq = subq_b
                source_name = "source B"
            else:
                raise ValueError(
                    f"Invalid table alias '{table_alias}' in column '{source_col}'",
                )

            if hasattr(subq.c, actual_col):
                select_columns.append(
                    getattr(subq.c, actual_col).label(label),
                )
                # Also include metadata columns if they exist
                # Check for all possible metadata suffixes with table prefixes
                metadata_suffixes = [
                    "__orchestra__source",
                    "__derived_log__equation",
                    "__derived_log__referenced_logs",
                ]
                for suffix in metadata_suffixes:
                    metadata_col_name = f"{actual_col}{suffix}"
                    if hasattr(subq.c, metadata_col_name):
                        select_columns.append(
                            getattr(subq.c, metadata_col_name).label(
                                f"{label}{suffix}",
                            ),
                        )
            else:
                raise ValueError(f"Column '{actual_col}' not found in {source_name}")
    else:
        # Select all columns from both tables, prefixing to avoid name clashes
        select_columns.extend(
            [
                getattr(subq_a.c, col_name).label(f"A_{col_name}")
                for col_name in subq_a.c.keys()
                if col_name != "log_event_id"
            ],
        )
        select_columns.extend(
            [
                getattr(subq_b.c, col_name).label(f"B_{col_name}")
                for col_name in subq_b.c.keys()
                if col_name != "log_event_id"
            ],
        )

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
) -> List[int]:
    """
    Creates new log entries from joined query results.

    Important: result_rows contain JSONB values from Log/DerivedLog tables, not numpy arrays.
    Embeddings are stored separately in the Embedding table and need to be recreated from
    the JSONB lists when copying derived logs that were created with embed().

    Args:
        result_rows: Result rows from the join query (contain JSONB values)
        project_id: ID of the project
        context_id: ID of the context
        field_type_dao: FieldTypeDAO instance for field type operations
        context_dao: ContextDAO instance for context operations
        session: SQLAlchemy session
        source_contexts: Optional mapping of source aliases to context IDs

    Returns:
        List of IDs of the newly created log events
    """
    new_log_ids = []
    now = datetime.now(timezone.utc)

    # Get the context object
    context_obj = session.get(Context, context_id)

    # Helper function to extract metadata from column names
    def extract_metadata(col_name: str) -> tuple:
        """Extract table and metadata type from column name."""
        if "__orchestra__source" in col_name:
            return "orchestra", "source"
        elif "__derived_log__equation" in col_name:
            return "derived_log", "equation"
        elif "__derived_log__referenced_logs" in col_name:
            return "derived_log", "referenced_logs"
        return None, None

    # Prepare collections for bulk operations
    new_field_types = []
    log_events = []
    log_event_contexts = []
    logs = []
    log_event_logs = []
    derived_logs = []
    log_event_derived_logs = []
    json_logs = []
    embeddings_to_create = []  # Track embeddings that need to be created

    # Process each row
    for row in result_rows:
        # Separate regular columns from metadata
        row_dict = {}
        metadata_dict = {}
        for col in row._fields:
            value = getattr(row, col)
            if col != "id":  # Skip the id column
                table, meta_type = extract_metadata(col)
                if table:
                    # This is a metadata column
                    metadata_dict[col] = value
                else:
                    # Regular data column - store original value (including numpy arrays)
                    row_dict[col] = value

        # Create a new LogEvent
        log_event = LogEvent(
            project_id=project_id,
            created_at=now,
            updated_at=now,
        )
        log_events.append(log_event)

        # We need to flush to get the ID before creating related records
        session.add(log_event)

    # Flush to get IDs
    session.flush()

    # Now create the related records with the generated IDs
    for i, log_event in enumerate(log_events):
        row = result_rows[i]
        # Separate regular columns from metadata - reuse the same logic
        row_dict = {}
        metadata_dict = {}
        for col in row._fields:
            value = getattr(row, col)
            if col != "id":  # Skip the id column
                table, meta_type = extract_metadata(col)
                if table:
                    # This is a metadata column
                    metadata_dict[col] = value
                else:
                    # Regular data column - store original value (including numpy arrays)
                    row_dict[col] = value

        # Create LogEventContext association
        log_event_contexts.append(
            LogEventContext(
                log_event_id=log_event.id,
                context_id=context_id,
            ),
        )

        # Create individual Log entries for each column in the joined result
        # Check metadata to determine if fields should go to Embedding table
        for col, val in row_dict.items():
            # Skip metadata columns
            if extract_metadata(col)[0]:
                continue

            # Get source metadata for this field
            source_info = metadata_dict.get(f"{col}__orchestra__source")
            equation_info = metadata_dict.get(f"{col}__derived_log__equation")
            referenced_logs_info = metadata_dict.get(
                f"{col}__derived_log__referenced_logs",
            )

            # Look up the original field type from any source context
            original_field_type = None

            # Try to find the field type in any source context
            if source_contexts:
                for alias, src_context_id in source_contexts.items():
                    field_type = field_type_dao.get_by_name_and_context(
                        project_id=project_id,
                        field_name=col,
                        context_id=src_context_id,
                    )
                    if field_type:
                        original_field_type = field_type
                        break

            # Also check if it exists in the current project (global field types)
            if not original_field_type:
                field_type = field_type_dao.get_by_name_and_context(
                    project_id=project_id,
                    field_name=col,
                    context_id=None,  # Global field type
                )
                if field_type:
                    original_field_type = field_type

            # Check if field already exists in target context
            existing_field_type = field_type_dao.get_by_name_and_context(
                project_id=project_id,
                field_name=col,
                context_id=context_id,
            )

            if existing_field_type:
                # Validate type consistency
                entered_type = LogDAO.infer_type(col, val)
                if (
                    existing_field_type.field_type != "NoneType"
                    and entered_type != "NoneType"
                ):
                    if entered_type != existing_field_type.field_type:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Type mismatch for field '{col}' in joined result: expected {existing_field_type.field_type}, got {entered_type}",
                        )
            elif original_field_type:
                # Copy the original field type to the new context
                # Determine the field category based on whether it's a derived log
                field_category = (
                    "derived_entry"
                    if source_info == "derived_log"
                    else original_field_type.field_category
                )

                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": col,
                        "value": val,
                        "mutable": original_field_type.mutable,
                        "unique": original_field_type.unique,
                        "field_category": field_category,
                        "enum_values": original_field_type.enum_values,
                        "enum_restrict": original_field_type.enum_restrict,
                        "description": original_field_type.description,
                        "context_id": context_id,
                    },
                )
            else:
                # No original field type found, infer from value
                # Determine field category based on source
                field_category = (
                    "derived_entry" if source_info == "derived_log" else "entry"
                )

                # Only make it mutable if the context is versioned
                mutable = context_obj and context_obj.is_versioned

                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": col,
                        "value": val,
                        "mutable": mutable,
                        "unique": False,
                        "field_category": field_category,
                        "context_id": context_id,
                    },
                )

            # Values from result_rows are already JSONB from Log/DerivedLog tables
            # No need to encode - they're already properly formatted
            inferred_type = LogDAO.infer_type(col, val)

            # Check if this was originally a derived log
            if source_info == "derived_log" and equation_info:
                # Create a DerivedLog entry with the JSONB value as-is
                derived_log = DerivedLog(
                    key=col,
                    value=val,  # Already JSONB from the query
                    equation=equation_info,
                    referenced_logs=referenced_logs_info,
                    inferred_type=inferred_type,
                    created_at=now,
                    updated_at=now,
                )
                derived_logs.append(derived_log)
                session.add(derived_log)
            else:
                # Create a regular Log entry
                log = Log(
                    key=col,
                    value=val,  # Already JSONB from the query
                    inferred_type=inferred_type,
                    created_at=now,
                    updated_at=now,
                )
                logs.append(log)
                session.add(log)

            # If value is a dict or list, create a JSONLog entry
            if isinstance(val, (dict, list)):
                json_logs.append(
                    {
                        "log_event_id": log_event.id,  # Store temporarily for association
                        "key": col,
                        "value": val,  # Already JSONB
                    },
                )

        new_log_ids.append(log_event.id)

    # Flush to get Log and DerivedLog IDs
    session.flush()

    # Look up embeddings from original log events
    # Get all source log event IDs
    source_log_event_ids = set()
    for row in result_rows:
        log_event_id_a = getattr(row, "log_event_id_a", None)
        log_event_id_b = getattr(row, "log_event_id_b", None)
        if log_event_id_a:
            source_log_event_ids.add(log_event_id_a)
        if log_event_id_b:
            source_log_event_ids.add(log_event_id_b)

    # Query all embeddings from source log events in one go
    if source_log_event_ids:
        source_embeddings = (
            session.query(Embedding)
            .filter(
                Embedding.ref_id.in_(source_log_event_ids),
            )
            .all()
        )

        # Build a lookup map: (ref_id, key) -> embedding
        embedding_lookup = {}
        for emb in source_embeddings:
            embedding_lookup[(emb.ref_id, emb.key)] = emb

        # Track which fields are derived logs for each row
        for i, (log_event, row) in enumerate(zip(log_events, result_rows)):
            log_event_id_a = getattr(row, "log_event_id_a", None)
            log_event_id_b = getattr(row, "log_event_id_b", None)

            # Rebuild metadata dict for this row
            metadata_dict = {}
            for col in row._fields:
                table, meta_type = extract_metadata(col)
                if table:
                    metadata_dict[col] = getattr(row, col)

            # Check each field in this row
            for col in row._fields:
                if (
                    col == "id"
                    or extract_metadata(col)[0]
                    or col in ["log_event_id_a", "log_event_id_b"]
                ):
                    continue

                # Get source metadata for this field
                source_info = metadata_dict.get(f"{col}__orchestra__source")

                # Only process derived log fields
                if source_info == "derived_log":
                    # Try to find embedding for this key from either source
                    for source_id in [log_event_id_a, log_event_id_b]:
                        if source_id:
                            # Try different key variations to handle aliasing
                            # Remove common prefixes
                            if col.startswith("A_") or col.startswith("B_"):
                                base_key = col[2:]
                            else:
                                base_key = col

                            # Check if embedding exists for this key
                            embedding = embedding_lookup.get((source_id, base_key))
                            if not embedding:
                                embedding = embedding_lookup.get((source_id, col))

                            if embedding:
                                embeddings_to_create.append(
                                    {
                                        "log_event_id": log_event.id,
                                        "key": col,
                                        "vector": embedding.vector,
                                        "model": embedding.model,
                                    },
                                )
                                break  # Found embedding for this key

    # Create associations
    log_idx = 0
    derived_log_idx = 0
    for i, (log_event, row) in enumerate(zip(log_events, result_rows)):
        # Rebuild metadata dict for this row
        metadata_dict = {}
        for col in row._fields:
            table, meta_type = extract_metadata(col)
            if table:
                metadata_dict[col] = getattr(row, col)

        # Create associations for each field
        for col in row._fields:
            if col == "id" or extract_metadata(col)[0]:
                continue

            # Check if this is a derived log
            source_info = metadata_dict.get(f"{col}__orchestra__source")

            if source_info == "derived_log" and derived_log_idx < len(derived_logs):
                # Create association for derived log
                derived_log = derived_logs[derived_log_idx]
                log_event_derived_logs.append(
                    LogEventDerivedLog(
                        log_event_id=log_event.id,
                        derived_log_id=derived_log.id,
                    ),
                )
                derived_log_idx += 1
            elif log_idx < len(logs):
                # Create association for regular log
                log = logs[log_idx]
                log_event_logs.append(
                    LogEventLog(
                        log_event_id=log_event.id,
                        log_id=log.id,
                    ),
                )
                log_idx += 1

    # Bulk create new field types if any
    try:
        if new_field_types:
            field_type_dao.bulk_create_field_types(new_field_types)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Bulk insert related records
    session.bulk_save_objects(log_event_contexts)
    session.bulk_save_objects(log_event_logs)
    session.bulk_save_objects(log_event_derived_logs)

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

    # Handle JSONLog creation with many-to-many relationship
    if json_logs:
        # Create JSONLog objects without log_event_id
        json_log_objects = []
        json_log_associations = []  # Track which log_event_id goes with each JSONLog

        for json_log_data in json_logs:
            log_event_id = json_log_data["log_event_id"]
            json_log = JSONLog(
                key=json_log_data["key"],
                value=json_log_data["value"],
            )
            json_log_objects.append(json_log)
            json_log_associations.append(log_event_id)
            session.add(json_log)

        # Flush to get JSONLog IDs
        session.flush()

        # Create LogEventJSONLog associations
        log_event_json_logs = []
        for i, json_log in enumerate(json_log_objects):
            log_event_json_logs.append(
                LogEventJSONLog(
                    log_event_id=json_log_associations[i],
                    json_log_id=json_log.id,
                ),
            )

        session.bulk_save_objects(log_event_json_logs)

    return new_log_ids


def _create_logs_by_reference(
    result_rows,
    project_id: int,
    context_id: int,
    columns: Optional[Dict[str, str]],
    session,
) -> List[int]:
    """
    Creates new log events that reference existing logs from joined query results.
    Columns are not aliased, so we reference the original logs by key.

    Args:
        result_rows: Result rows from the join query containing log_event_id_a and log_event_id_b
        project_id: ID of the project
        context_id: ID of the context
        columns: Dictionary mapping source columns to new column names
        session: SQLAlchemy session

    Returns:
        List of IDs of the newly created log events
    """
    new_log_ids = []
    now = datetime.now(timezone.utc)

    # Prepare collections for bulk operations
    log_events = []
    log_event_contexts = []
    log_event_logs = []
    log_event_derived_logs = []

    # Process each row
    for row in result_rows:
        # Create a new LogEvent
        log_event = LogEvent(
            project_id=project_id,
            created_at=now,
            updated_at=now,
        )
        log_events.append(log_event)
        session.add(log_event)

    # Flush to get IDs
    session.flush()

    # Now create the related records with the generated IDs
    for i, log_event in enumerate(log_events):
        row = result_rows[i]

        # Create LogEventContext association
        log_event_contexts.append(
            LogEventContext(
                log_event_id=log_event.id,
                context_id=context_id,
            ),
        )

        # Get source LogEvent IDs
        log_event_id_a = getattr(row, "log_event_id_a", None)
        log_event_id_b = getattr(row, "log_event_id_b", None)

        # Process columns that should be included
        if columns:
            # Convert to list if it's still a dict (shouldn't happen with validation)
            columns_list = (
                columns if isinstance(columns, list) else list(columns.keys())
            )

            # Only include columns specified in the list
            for source_col in columns_list:
                table_prefix, original_col = source_col.split(".", 1)
                source_log_event_id = (
                    log_event_id_a if table_prefix.upper() == "A" else log_event_id_b
                )

                # Find the original Log with this key from the source LogEvent
                original_log = (
                    session.query(Log)
                    .join(LogEventLog)
                    .filter(
                        LogEventLog.log_event_id == source_log_event_id,
                        Log.key == original_col,
                    )
                    .first()
                )

                # Check if it's a derived log
                original_derived_log = (
                    session.query(DerivedLog)
                    .join(LogEventDerivedLog)
                    .filter(
                        LogEventDerivedLog.log_event_id == source_log_event_id,
                        DerivedLog.key == original_col,
                    )
                    .first()
                )

                if original_log:
                    # For pass-by-reference, we reference the original log
                    # Check if this association already exists to avoid duplicates
                    existing = any(
                        lel.log_id == original_log.id
                        for lel in log_event_logs
                        if lel.log_event_id == log_event.id
                    )
                    if not existing:
                        log_event_logs.append(
                            LogEventLog(
                                log_event_id=log_event.id,
                                log_id=original_log.id,
                            ),
                        )
                elif original_derived_log:
                    # Check if this association already exists
                    existing = any(
                        ledl.derived_log_id == original_derived_log.id
                        for ledl in log_event_derived_logs
                        if ledl.log_event_id == log_event.id
                    )
                    if not existing:
                        log_event_derived_logs.append(
                            LogEventDerivedLog(
                                log_event_id=log_event.id,
                                derived_log_id=original_derived_log.id,
                            ),
                        )
        else:
            # Include all columns with default naming
            for col in row._fields:
                # Skip the log_event_id columns
                if col in ("log_event_id_a", "log_event_id_b"):
                    continue

                value = getattr(row, col)
                if value is not None:
                    # Determine which source this column came from
                    source_log_event_id = None
                    original_col = col

                    # Default naming convention: A_column or B_column
                    if col.startswith("A_"):
                        source_log_event_id = log_event_id_a
                        original_col = col[2:]  # Remove 'A_' prefix
                    elif col.startswith("B_"):
                        source_log_event_id = log_event_id_b
                        original_col = col[2:]  # Remove 'B_' prefix

                    if source_log_event_id:
                        # Find the Log with this key from the source LogEvent
                        log = (
                            session.query(Log)
                            .join(LogEventLog)
                            .filter(
                                LogEventLog.log_event_id == source_log_event_id,
                                Log.key == original_col,
                            )
                            .first()
                        )

                        # Check if it's a derived log
                        derived_log = (
                            session.query(DerivedLog)
                            .join(LogEventDerivedLog)
                            .filter(
                                LogEventDerivedLog.log_event_id == source_log_event_id,
                                DerivedLog.key == original_col,
                            )
                            .first()
                        )

                        if log:
                            # Reference the existing log
                            log_event_logs.append(
                                LogEventLog(
                                    log_event_id=log_event.id,
                                    log_id=log.id,
                                ),
                            )
                        elif derived_log:
                            log_event_derived_logs.append(
                                LogEventDerivedLog(
                                    log_event_id=log_event.id,
                                    derived_log_id=derived_log.id,
                                ),
                            )

        new_log_ids.append(log_event.id)

    # No need to flush or create associations for new logs since we're not creating any

    # Bulk insert related records
    session.bulk_save_objects(log_event_contexts)
    session.bulk_save_objects(log_event_logs)
    session.bulk_save_objects(log_event_derived_logs)

    return new_log_ids


def _join_logs(
    project_id: int,
    project_name: str,
    pair_of_args: List[Dict[str, Any]],
    join_expr: str,
    mode: str,
    context_id: int,
    columns: Optional[Union[Dict[str, str], List[str]]] = None,
    copy: bool = False,
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

    Args:
        project_id: ID of the project containing the logs
        project_name: Name of the project
        pair_of_args: List of two dictionaries containing filtering criteria for logs to join
        join_expr: SQL expression for the join condition using aliases A and B
                   (e.g., 'A.user_id = B.user_id')
        mode: Type of join to perform ('inner', 'left', 'right', or 'outer')
        context_id: ID of the context where joined logs will be stored
        columns: Optional column specification. Can be either:
                 - Dictionary mapping source columns to new column names (only with copy=True):
                   {'A.column_name': 'new_name', 'B.column_name': 'other_name'}
                 - List of source columns to include (required with copy=False):
                   ['A.column_name', 'B.column_name']
        copy: If True, creates copies of the logs. If False (default), references existing logs.
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
        # Build subqueries for both sets of filtering criteria
        context_a = pair_of_args[0].get("context")
        context_b = pair_of_args[1].get("context")
        if not context_a or not context_b:
            raise ValueError(
                f"Contexts for both queries must be provided in the pair of args. Got: {context_a} and {context_b}",
            )

        filter_expr_a = pair_of_args[0].get("filter_expr")
        filter_expr_b = pair_of_args[1].get("filter_expr")
        if filter_expr_a:
            pair_of_args[0]["filter_expr"] = filter_expr_a.replace(context_a + ".", "")
        if filter_expr_b:
            pair_of_args[1]["filter_expr"] = filter_expr_b.replace(context_b + ".", "")

        # Validate columns format based on copy parameter
        if not copy and columns is not None and isinstance(columns, dict):
            raise ValueError(
                "When copy=False (pass-by-reference), column aliases are not supported. "
                "Please provide columns as a list of column names instead of a dictionary.",
            )

        # replace context_a with 'A' alias and context_b with 'B' alias
        join_expr = join_expr.replace(context_a, "A").replace(context_b, "B")
        if columns is not None:
            if isinstance(columns, dict):
                # Dictionary format - process aliases
                new_columns = {}
                for source_col, new_alias in columns.items():
                    processed_source_col = source_col.replace(context_a, "A").replace(
                        context_b,
                        "B",
                    )
                    new_columns[processed_source_col] = new_alias
                columns = new_columns
            elif isinstance(columns, list):
                # List format - just replace context names
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
                    "columns must be either a dictionary (for aliasing with copy=True) "
                    "or a list (for column selection with copy=False)",
                )

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

        # Construct the join query
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

        # Create new log entries from the joined results
        if copy:
            # Create copies of the logs
            new_log_ids = _create_logs_from_joined_rows(
                result_rows=result_rows,
                project_id=project_id,
                context_id=context_id,
                field_type_dao=field_type_dao,
                context_dao=context_dao,
                session=session,
                source_contexts=source_contexts,
            )
        else:
            # Reference existing logs
            new_log_ids = _create_logs_by_reference(
                result_rows=result_rows,
                project_id=project_id,
                context_id=context_id,
                columns=columns,
                session=session,
            )

        # Commit the transaction
        session.commit()

        return new_log_ids

    except Exception as e:
        raise ValueError(f"Failed to join logs: {traceback.format_exc()}")
