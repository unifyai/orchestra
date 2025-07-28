import json
import logging
import random
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import (
    JSON,
    Integer,
    String,
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
    true,
    union_all,
)
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
    JSONLog,
    JSONLogHistory,
    Log,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.settings import settings
from orchestra.web.api.log.python2SQL.operators import _create_truthiness_condition
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.utils.http_responses import not_found

from ..python2SQL import STR_TO_SQL_TYPES
from ..python2SQL.core import build_sql_query
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
    start_time = time.time()
    logging.info(
        f"[_paginate_events] Starting pagination - limit: {limit}, offset: {offset}, has_joins: {has_joins}, randomize: {randomize}",
    )

    # If we have joins (for sorting), we need to handle differently
    if has_joins and order_by_cols:
        logging.info(
            f"[_paginate_events] Processing joined query with {len(order_by_cols)} order columns",
        )

        # Build paginated query with joins preserved (use optimized versions)
        query_build_start = time.time()
        pag_query = base_event_q.add_columns(
            func.row_number().over(order_by=order_by_cols).label("row_num"),
        ).order_by(*order_by_cols)

        if limit:
            pag_query = pag_query.limit(limit)
            logging.info(f"[_paginate_events] Applied limit: {limit}")
        if offset:
            pag_query = pag_query.offset(offset)
            logging.info(f"[_paginate_events] Applied offset: {offset}")

        logging.info(
            f"[_paginate_events] Joined query built in {time.time() - query_build_start:.3f}s",
        )

        total_time = time.time() - start_time
        logging.info(
            f"[_paginate_events] Joined pagination completed in {total_time:.3f}s",
        )
        return pag_query.subquery("paginated_ids_subq")

    # Original logic for simple queries
    logging.info(f"[_paginate_events] Processing simple query (non-joined)")

    subquery_start = time.time()
    relevant_sq = base_event_q.subquery("relevant_log_events")
    logging.info(
        f"[_paginate_events] Relevant subquery created in {time.time() - subquery_start:.3f}s",
    )

    # Get total count with a cheap index scan
    count_start = time.time()
    total_count = session.query(func.count()).select_from(relevant_sq).scalar()
    logging.info(
        f"[_paginate_events] Count query completed in {time.time() - count_start:.3f}s - total_count: {total_count}",
    )

    # Build the ordered/limited ID list
    ordering_start = time.time()
    if randomize:
        random_key = func.md5(cast(relevant_sq.c.id, String) + literal(seed))
        order_by_cols = [random_key]
        logging.info(f"[_paginate_events] Applied randomization with seed: {seed}")
    if not order_by_cols:
        order_by_cols = [desc(relevant_sq.c.id)]
        logging.info(f"[_paginate_events] Applied default ordering (desc by id)")

    logging.info(
        f"[_paginate_events] Ordering setup completed in {time.time() - ordering_start:.3f}s with {len(order_by_cols)} order columns",
    )

    pagination_start = time.time()
    paginated_sq = select(
        relevant_sq.c.id.label("id"),
        func.row_number().over(order_by=order_by_cols).label("row_num"),
    ).order_by(*order_by_cols)

    if limit:
        paginated_sq = paginated_sq.limit(limit)
        logging.info(f"[_paginate_events] Applied limit: {limit}")
    if offset:
        paginated_sq = paginated_sq.offset(offset)
        logging.info(f"[_paginate_events] Applied offset: {offset}")

    logging.info(
        f"[_paginate_events] Pagination query built in {time.time() - pagination_start:.3f}s",
    )

    total_time = time.time() - start_time
    logging.info(f"[_paginate_events] Simple pagination completed in {total_time:.3f}s")
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
    return _build_unified_logs_subquery(
        session=session,
        relevant_log_events=ids_subq,
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

    return sort_expr


def _build_sort_clauses(
    session,
    log_event_query,
    field_types,
    sorting,
    unified_logs_for_sort,
    sort_val_sqs,
    sort_criteria,
):
    """
    Helper function to build sorting clauses for log queries.
    Extracts the sorting logic from _get_logs_query for reusability.
    """
    start_time = time.time()
    logging.debug(f"[_build_sort_clauses] Starting sort clauses construction")

    if sorting:
        parse_start = time.time()
        sort_dict = json.loads(sorting)
        logging.debug(
            f"[_build_sort_clauses] Sort expression parsed in {time.time() - parse_start:.3f}s - {len(sort_dict)} sort fields",
        )

        for i, (sort_key, mode) in enumerate(sort_dict.items()):
            sort_field_start = time.time()
            logging.debug(
                f"[_build_sort_clauses] Processing sort field {i+1}/{len(sort_dict)}: '{sort_key}' ({mode})",
            )

            if is_image_field(sort_key, field_types) or is_audio_field(
                sort_key,
                field_types,
            ):
                logging.debug(
                    f"[_build_sort_clauses] Skipping media field: '{sort_key}'",
                )
                continue
            if mode not in ("ascending", "descending"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Sort mode must be 'ascending' or 'descending', got {mode}.",
                )

            # Parse expression
            expr_parse_start = time.time()
            try:
                expr_dict = str_filter_exp_to_dict(
                    sort_key,
                    field_names=list(field_types.keys()),
                )
                logging.debug(
                    f"[_build_sort_clauses] Sort expression parsed in {time.time() - expr_parse_start:.3f}s",
                )
            except Exception:
                logging.error(
                    f"[_build_sort_clauses] Failed to parse sort expression: '{sort_key}'",
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid sort expression '{sort_key}'",
                )

            if expr_dict.get("type", None) == "identifier":
                # static field sorting
                logging.debug(
                    f"[_build_sort_clauses] Building static field sort for: '{sort_key}'",
                )
                static_sort_start = time.time()

                cast_expr = _build_sort_criteria(
                    unified_logs_for_sort.c.value,
                    sort_key,
                    field_types,
                )

                sort_val_sq = (
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

                sort_val_sqs.append(sort_val_sq)

                # remember ORDER‑BY expression
                direction = asc if mode == "ascending" else desc
                sort_criteria.append(direction(sort_val_sq.c.val).nulls_last())

                logging.debug(
                    f"[_build_sort_clauses] Static field sort built in {time.time() - static_sort_start:.3f}s",
                )
            else:
                # dynamic expression sorting
                logging.debug(
                    f"[_build_sort_clauses] Building dynamic expression sort for: '{sort_key}'",
                )
                dynamic_sort_start = time.time()

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

                logging.debug(
                    f"[_build_sort_clauses] Dynamic expression sort built in {time.time() - dynamic_sort_start:.3f}s",
                )

            logging.debug(
                f"[_build_sort_clauses] Sort field '{sort_key}' completed in {time.time() - sort_field_start:.3f}s",
            )

    total_time = time.time() - start_time
    logging.debug(
        f"[_build_sort_clauses] All sort clauses built in {total_time:.3f}s - created {len(sort_val_sqs)} subqueries",
    )


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
    start_time = time.time()
    logging.info(
        f"[_apply_post_filters] Starting filter application - from_ids: {from_ids is not None}, exclude_ids: {exclude_ids is not None}, from_fields: {from_fields is not None}, exclude_fields: {exclude_fields is not None}",
    )
    logging.info(
        f"[_apply_post_filters] Parameter filters - exclude_params: {exclude_params}, exclude_entries: {exclude_entries}",
    )

    # Validate ID filters
    validation_start = time.time()
    if from_ids and exclude_ids:
        logging.error(
            f"[_apply_post_filters] Invalid configuration: both from_ids and exclude_ids specified",
        )
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )
    logging.info(
        f"[_apply_post_filters] Filter validation completed in {time.time() - validation_start:.3f}s",
    )

    # Apply ID filters
    id_filter_start = time.time()
    if from_ids:
        include_ids = [int(x) for x in from_ids.split("&")]
        logging.info(
            f"[_apply_post_filters] Applying from_ids filter with {len(include_ids)} IDs",
        )
        base_q = base_q.filter(
            ul_table.c.log_event_id.in_(include_ids),
        )
    elif exclude_ids:
        exclude_set = [int(x) for x in exclude_ids.split("&")]
        logging.info(
            f"[_apply_post_filters] Applying exclude_ids filter with {len(exclude_set)} IDs",
        )
        base_q = base_q.filter(
            ul_table.c.log_event_id.notin_(exclude_set),
        )
    logging.info(
        f"[_apply_post_filters] ID filters applied in {time.time() - id_filter_start:.3f}s",
    )

    # Apply param/entry type filters
    type_filter_start = time.time()
    if exclude_params:
        logging.info(
            f"[_apply_post_filters] Excluding parameters (keeping only entries)",
        )
        base_q = base_q.filter(
            ul_table.c.param_version.is_(None),
        )
    elif exclude_entries:
        logging.info(
            f"[_apply_post_filters] Excluding entries (keeping only parameters)",
        )
        base_q = base_q.filter(
            ul_table.c.param_version.isnot(None),
        )
    logging.info(
        f"[_apply_post_filters] Type filters applied in {time.time() - type_filter_start:.3f}s",
    )

    # Validate field filters
    field_validation_start = time.time()
    if from_fields and exclude_fields:
        logging.error(
            f"[_apply_post_filters] Invalid configuration: both from_fields and exclude_fields specified",
        )
        raise HTTPException(
            status_code=400,
            detail="Only one of from_fields or exclude_fields can be set.",
        )
    logging.info(
        f"[_apply_post_filters] Field filter validation completed in {time.time() - field_validation_start:.3f}s",
    )

    # Apply field filters
    field_filter_start = time.time()
    if from_fields:
        allowed_fields = from_fields.split("&")
        logging.info(
            f"[_apply_post_filters] Applying from_fields filter with {len(allowed_fields)} fields: {allowed_fields}",
        )
        base_q = base_q.filter(
            ul_table.c.key.in_(allowed_fields),
        )
    elif exclude_fields:
        excluded_fields = exclude_fields.split("&")
        logging.info(
            f"[_apply_post_filters] Applying exclude_fields filter with {len(excluded_fields)} fields: {excluded_fields}",
        )
        base_q = base_q.filter(
            ul_table.c.key.notin_(excluded_fields),
        )
    logging.info(
        f"[_apply_post_filters] Field filters applied in {time.time() - field_filter_start:.3f}s",
    )

    total_time = time.time() - start_time
    logging.info(
        f"[_apply_post_filters] All filters applied successfully in {total_time:.3f}s",
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
    start_time = time.time()
    phase_timings = {}  # Track timing for each phase
    logging.info(
        f"[_get_logs_query] Starting query - project: {project}, context: {context}, limit: {limit}, offset: {offset}",
    )
    user_id = request_fastapi.state.user_id

    # 1) Validate the project
    phase_start = time.time()
    logging.debug(
        f"[_get_logs_query] Phase 1: Validating project '{project}' for user {user_id}",
    )
    try:
        project_id = project_dao.get_by_user_and_name(name=project, user_id=user_id).id
        logging.debug(
            f"[_get_logs_query] Project validation successful - project_id: {project_id}",
        )
    except (IndexError, AttributeError):
        logging.error(
            f"[_get_logs_query] Project validation failed - project '{project}' not found for user {user_id}",
        )
        raise not_found(f"Project {project}")
    phase_duration = time.time() - phase_start
    phase_timings["Phase 1: Project Validation"] = phase_duration
    logging.debug(f"[_get_logs_query] Phase 1 completed in {phase_duration:.3f}s")

    # Phase 1: filtering, sorting, pagination, etc.
    phase_start = time.time()
    logging.debug(
        f"[_get_logs_query] Phase 2: Setting up log event query and context processing",
    )
    log_event_query = session.query(LogEvent.id).filter(
        LogEvent.project_id == project_id,
    )
    context_name = "" if not context else context
    logging.debug(f"[_get_logs_query] Processing context: '{context_name}'")
    context_obj = context_dao.filter(name=context_name, project_id=project_id)
    if context_obj:
        context_id = context_obj[0][0].id
        logging.debug(f"[_get_logs_query] Context found - context_id: {context_id}")
        log_event_query = log_event_query.join(LogEventContext).filter(
            LogEventContext.context_id == context_id,
        )
    else:
        context_id = None
        logging.debug(f"[_get_logs_query] No context found - using context_id: None")

    field_types_start = time.time()
    field_types = field_type_dao.get_field_types(project_id, context_id=context_id)
    logging.debug(
        f"[_get_logs_query] Retrieved {len(field_types)} field types in {time.time() - field_types_start:.3f}s",
    )
    phase_duration = time.time() - phase_start
    phase_timings["Phase 2: Context & Field Types"] = phase_duration
    logging.debug(f"[_get_logs_query] Phase 2 completed in {phase_duration:.3f}s")

    if filter_expr:
        phase_start = time.time()
        logging.debug(
            f"[_get_logs_query] Phase 3: Processing filter expression: '{filter_expr}'",
        )
        try:
            filter_parse_start = time.time()
            filter_dict = str_filter_exp_to_dict(
                filter_expr,
                field_names=list(field_types.keys()),
            )
            logging.debug(
                f"[_get_logs_query] Filter expression parsed in {time.time() - filter_parse_start:.3f}s",
            )
        except Exception as e:
            logging.error(
                f"[_get_logs_query] Filter expression parsing failed: {str(e)}",
            )
            session.rollback()
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filter expression: {str(e)}",
            )

        if filter_dict:
            logging.debug(
                f"[_get_logs_query] Filter dict created - operand: {filter_dict.get('operand', 'N/A')}",
            )

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
                            ):
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Field '{field}' is a media type and can only be used with 'exists' or 'isNone' operator",
                                )
                    for k, v in fd.items():
                        if isinstance(v, dict):
                            validate_filter_dict.parent = fd
                            validate_filter_dict(v)

            # Define a subquery for event IDs to pass to the query builder
            event_ids_subq = log_event_query.subquery(name="event_ids_subq")

            try:
                filter_apply_start = time.time()
                # --- OPTIMIZATION FOR 'OR' ---
                if isinstance(filter_dict, dict) and filter_dict.get("operand") == "or":
                    logging.debug(f"[_get_logs_query] Applying OR filter optimization")
                    or_conditions = flatten_or_conditions(filter_dict)
                    logging.debug(
                        f"[_get_logs_query] Found {len(or_conditions)} OR conditions",
                    )
                    matching_id_subqueries = []

                    for i, condition_dict in enumerate(or_conditions):
                        condition_start = time.time()
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
                        logging.debug(
                            f"[_get_logs_query] OR condition {i+1} processed in {time.time() - condition_start:.3f}s",
                        )

                    if matching_id_subqueries:
                        union_start = time.time()
                        unioned_ids_subq = union_all(*matching_id_subqueries).subquery()
                        log_event_query = log_event_query.filter(
                            LogEvent.id.in_(select(unioned_ids_subq)),
                        )
                        logging.debug(
                            f"[_get_logs_query] OR union completed in {time.time() - union_start:.3f}s",
                        )

                # --- OPTIMIZATION FOR 'AND' ---
                elif (
                    isinstance(filter_dict, dict)
                    and filter_dict.get("operand") == "and"
                ):
                    logging.debug(f"[_get_logs_query] Applying AND filter optimization")
                    and_conditions = flatten_and_conditions(filter_dict)
                    logging.debug(
                        f"[_get_logs_query] Found {len(and_conditions)} AND conditions",
                    )

                    for i, condition_dict in enumerate(and_conditions):
                        condition_start = time.time()
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
                        logging.debug(
                            f"[_get_logs_query] AND condition {i+1} processed in {time.time() - condition_start:.3f}s",
                        )

                # --- FALLBACK FOR SINGLE CONDITIONS OR OTHER OPERATORS ---
                else:
                    logging.debug(
                        f"[_get_logs_query] Applying fallback filter processing",
                    )
                    fallback_start = time.time()
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
                    logging.debug(
                        f"[_get_logs_query] Fallback filter completed in {time.time() - fallback_start:.3f}s",
                    )

                logging.debug(
                    f"[_get_logs_query] All filter processing completed in {time.time() - filter_apply_start:.3f}s",
                )

            except Exception as e:
                logging.error(f"[_get_logs_query] Filter processing error: {str(e)}")
                session.rollback()
                # Provide detailed error information
                error_msg = f"Error processing filter expression: {str(e)}"
                if hasattr(e, "__class__"):
                    error_msg = f"{e.__class__.__name__}: {error_msg}"
                raise HTTPException(
                    status_code=400,
                    detail=error_msg,
                )

        phase_duration = time.time() - phase_start
        phase_timings["Phase 3: Filter Processing"] = phase_duration
        logging.debug(
            f"[_get_logs_query] Phase 3 (filter processing) completed in {phase_duration:.3f}s",
        )

    # Apply from_ids/exclude_ids filters early since they filter on log_event_id
    phase_start = time.time()
    logging.debug(
        f"[_get_logs_query] Phase 4: Applying ID filters - from_ids: {from_ids is not None}, exclude_ids: {exclude_ids is not None}",
    )
    if from_ids and exclude_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )

    if from_ids:
        include_ids = [int(x) for x in from_ids.split("&")]
        logging.debug(
            f"[_get_logs_query] Applying from_ids filter with {len(include_ids)} IDs",
        )
        log_event_query = log_event_query.filter(
            LogEvent.id.in_(include_ids),
        )
    elif exclude_ids:
        exclude_set = [int(x) for x in exclude_ids.split("&")]
        logging.debug(
            f"[_get_logs_query] Applying exclude_ids filter with {len(exclude_set)} IDs",
        )
        log_event_query = log_event_query.filter(
            LogEvent.id.notin_(exclude_set),
        )
    phase_duration = time.time() - phase_start
    phase_timings["Phase 4: ID Filters"] = phase_duration
    logging.debug(f"[_get_logs_query] Phase 4 completed in {phase_duration:.3f}s")

    # Apply field filters at log event level
    phase_start = time.time()
    logging.debug(
        f"[_get_logs_query] Phase 5: Applying field filters - from_fields: {from_fields is not None}, exclude_fields: {exclude_fields is not None}",
    )
    if from_fields and exclude_fields:
        raise HTTPException(
            status_code=400,
            detail="Only one of from_fields or exclude_fields can be set.",
        )

    if from_fields:
        # Filter to only include log events that have at least one of the specified fields
        allowed_fields = from_fields.split("&")
        logging.debug(
            f"[_get_logs_query] Applying from_fields filter with {len(allowed_fields)} fields: {allowed_fields}",
        )
        # Check both Log and DerivedLog tables for matching fields
        log_exists = (
            session.query(Log.log_event_id)
            .filter(
                Log.log_event_id == LogEvent.id,
                Log.key.in_(allowed_fields),
            )
            .exists()
        )
        derived_log_exists = (
            session.query(DerivedLog.log_event_id)
            .filter(
                DerivedLog.log_event_id == LogEvent.id,
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
        logging.debug(
            f"[_get_logs_query] Applying exclude_fields filter with {len(excluded_fields)} fields: {excluded_fields}",
        )
        # Check both Log and DerivedLog tables for non-excluded fields
        log_exists = (
            session.query(Log.log_event_id)
            .filter(
                Log.log_event_id == LogEvent.id,
                Log.key.notin_(excluded_fields),
            )
            .exists()
        )
        derived_log_exists = (
            session.query(DerivedLog.log_event_id)
            .filter(
                DerivedLog.log_event_id == LogEvent.id,
                DerivedLog.key.notin_(excluded_fields),
            )
            .exists()
        )
        log_event_query = log_event_query.filter(
            or_(log_exists, derived_log_exists),
        )
    phase_duration = time.time() - phase_start
    phase_timings["Phase 5: Field Filters"] = phase_duration
    logging.debug(f"[_get_logs_query] Phase 5 completed in {phase_duration:.3f}s")

    # FIXME: potential duplicate logic
    phase_start = time.time()
    logging.debug(f"[_get_logs_query] Phase 6: Context validation and sorting setup")
    if context:
        context_obj = context_dao.filter(name=context, project_id=project_id)
    else:
        context_obj = context_dao.filter(name="", project_id=project_id)
        if not context_obj:
            if latest_timestamp:
                project_obj = project_dao.filter(name=project, user_id=user_id)
                logging.debug(
                    f"[_get_logs_query] Returning latest timestamp for project without context",
                )
                total_time = time.time() - start_time
                logging.info(
                    f"[_get_logs_query] Early return (latest timestamp) - total time: {total_time:.3f}s",
                )
                return project_obj[0][0].created_at.isoformat()
            else:
                logging.debug(
                    f"[_get_logs_query] No context found, returning empty result",
                )
                total_time = time.time() - start_time
                logging.info(
                    f"[_get_logs_query] Early return (no context) - total time: {total_time:.3f}s",
                )
                return [], 0, 0

    if not context_obj:
        logging.error(f"[_get_logs_query] Context '{context}' not found")
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context}' not found",
        )
    context_obj = context_obj[0][0]
    ctx_id_val = context_obj.id
    logging.debug(f"[_get_logs_query] Using context_id: {ctx_id_val}")

    # ---- Phase-1: gather all event IDs that match user filters ------------
    # Note: filter_expr has already been applied to log_event_query

    # Build ORDER BY expressions
    sort_val_sqs: List[Subquery] = []
    sort_criteria: List[Any] = []

    if not randomize and sorting:
        logging.debug(
            f"[_get_logs_query] Setting up sorting with expression: {sorting}",
        )
        sort_start = time.time()

        # Step 1: Create relevant log events subquery
        subquery_start = time.time()
        relevant_log_events = log_event_query.subquery(name="relevant_log_events")
        logging.debug(
            f"[_get_logs_query] [SORT] Relevant log events subquery created in {time.time() - subquery_start:.3f}s",
        )

        # Step 2: Build unified logs for sorting
        unified_start = time.time()
        unified_logs_for_sort = _build_unified_logs_subquery(
            session=session,
            relevant_log_events=relevant_log_events,
        )
        logging.debug(
            f"[_get_logs_query] [SORT] Unified logs for sorting built in {time.time() - unified_start:.3f}s",
        )

        # Step 3: Build sort clauses
        sort_clauses_start = time.time()
        _build_sort_clauses(
            session,
            log_event_query,
            field_types,
            sorting,
            unified_logs_for_sort,
            sort_val_sqs,
            sort_criteria,
        )
        logging.debug(
            f"[_get_logs_query] [SORT] Sort clauses built in {time.time() - sort_clauses_start:.3f}s - created {len(sort_val_sqs)} sort subqueries",
        )

        # Step 4: Add deterministic tie-breaker
        tiebreaker_start = time.time()
        sort_criteria.append(desc(relevant_log_events.c.id))
        logging.debug(
            f"[_get_logs_query] [SORT] Tie-breaker added in {time.time() - tiebreaker_start:.3f}s",
        )

        # Step 5: Join sort subqueries with log events
        join_start = time.time()
        joined_events = relevant_log_events
        for i, sq in enumerate(sort_val_sqs):
            sq_join_start = time.time()
            joined_events = joined_events.outerjoin(
                sq,
                sq.c.log_event_id == relevant_log_events.c.id,
            )
            logging.debug(
                f"[_get_logs_query] [SORT] Sort subquery {i+1} joined in {time.time() - sq_join_start:.3f}s",
            )
        logging.debug(
            f"[_get_logs_query] [SORT] All sort subqueries joined in {time.time() - join_start:.3f}s",
        )

        # Step 6: Build final query with sort info
        final_query_start = time.time()
        base_event_q = session.query(relevant_log_events.c.id).select_from(
            joined_events,
        )
        logging.debug(
            f"[_get_logs_query] [SORT] Final sorted query built in {time.time() - final_query_start:.3f}s",
        )

        total_sort_time = time.time() - sort_start
        logging.debug(
            f"[_get_logs_query] Sorting setup completed in {total_sort_time:.3f}s with {len(sort_val_sqs)} sort subqueries",
        )

        # For _paginate_events, we need to pass the joined query and sort criteria
        # This will ensure proper ordering without cartesian products
    else:
        # No sorting needed, just use the filtered events
        logging.debug(
            f"[_get_logs_query] No sorting required - randomize: {randomize}, sorting: {sorting is not None}",
        )
        base_event_q = log_event_query

    phase_duration = time.time() - phase_start
    phase_timings["Phase 6: Context & Sorting Setup"] = phase_duration
    logging.debug(f"[_get_logs_query] Phase 6 completed in {phase_duration:.3f}s")

    # ---- Phase-2: total_count + page -------------------------------
    phase_start = time.time()
    logging.info(
        f"[_get_logs_query] Phase 7: Pagination - limit: {limit}, offset: {offset}, randomize: {randomize}",
    )
    # Check if we have joins (when sorting is enabled)
    has_joins = bool(sorting) and not randomize
    logging.info(f"[_get_logs_query] Pagination with joins: {has_joins}")

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
    logging.info(f"[_get_logs_query] Pagination completed - total_count: {total_count}")
    phase_duration = time.time() - phase_start
    phase_timings["Phase 7: Pagination"] = phase_duration
    logging.info(f"[_get_logs_query] Phase 7 completed in {phase_duration:.3f}s")

    # Phase 3: Handle special cases
    if latest_timestamp:
        phase_start = time.time()
        logging.debug(
            f"[_get_logs_query] Phase 8: Handling latest_timestamp special case",
        )
        # Build unified logs only for timestamp check
        unified_logs_for_timestamp = _build_unified_logs_subquery(
            session=session,
            relevant_log_events=paginated_ids_subq,
        )
        max_updated_at = session.query(
            func.max(unified_logs_for_timestamp.c.updated_at),
        ).scalar()
        result = max_updated_at.isoformat() if max_updated_at else None
        logging.debug(f"[_get_logs_query] Latest timestamp result: {result}")
        phase_duration = time.time() - phase_start
        phase_timings["Phase 8: Latest Timestamp"] = phase_duration
        logging.debug(f"[_get_logs_query] Phase 8 completed in {phase_duration:.3f}s")
        return result

    # ---- Phase-4: build unified logs ONLY for the paginated IDs ----
    phase_start = time.time()
    logging.debug(
        f"[_get_logs_query] Phase 9: Building unified logs for paginated results",
    )
    unified_logs_limited = _build_unified_logs_limited(
        session,
        paginated_ids_subq,
    )
    phase_duration = time.time() - phase_start
    phase_timings["Phase 9: Build Unified Logs"] = phase_duration
    logging.debug(f"[_get_logs_query] Phase 9 completed in {phase_duration:.3f}s")

    phase_start = time.time()
    logging.info(
        f"[_get_logs_query] Phase 10: Applying final filters and column context processing",
    )
    filtered_logs_q = session.query(unified_logs_limited).filter(True)

    context_len = 0
    exclude_params = False
    exclude_entries = False
    if column_context is not None:
        logging.info(f"[_get_logs_query] Processing column_context: '{column_context}'")
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
        logging.info(
            f"[_get_logs_query] Column context processed - exclude_params: {exclude_params}, exclude_entries: {exclude_entries}, context_len: {context_len}",
        )
    if column_context:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_limited.c.key.startswith(column_context),
        )

    filter_start = time.time()
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
    logging.info(
        f"[_get_logs_query] Post filters applied in {time.time() - filter_start:.3f}s",
    )
    filtered_logs_subq = filtered_logs_q.subquery(name="filtered_logs_subq")

    # Get final logs - total_count already calculated in _paginate_events
    final_logs_start = time.time()
    raw_rows = _get_final_logs(session, filtered_logs_subq, paginated_ids_subq)
    logging.info(
        f"[_get_logs_query] Final logs retrieved - {len(raw_rows)} rows in {time.time() - final_logs_start:.3f}s",
    )

    result_processing_start = time.time()
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

    logging.info(
        f"[_get_logs_query] Result processing completed in {time.time() - result_processing_start:.3f}s",
    )
    phase_duration = time.time() - phase_start
    phase_timings["Phase 10: Final Processing"] = phase_duration
    logging.info(f"[_get_logs_query] Phase 10 completed in {phase_duration:.3f}s")

    total_time = time.time() - start_time

    # Sort phases by time taken (descending) and log the timing breakdown
    sorted_phases = sorted(phase_timings.items(), key=lambda x: x[1], reverse=True)
    logging.info(f"[_get_logs_query] TIMING BREAKDOWN (sorted by duration):")
    for phase_name, duration in sorted_phases:
        percentage = (duration / total_time) * 100 if total_time > 0 else 0
        logging.info(
            f"[_get_logs_query]   {phase_name}: {duration:.3f}s ({percentage:.1f}%)",
        )

    logging.info(
        f"[_get_logs_query] Query completed - total time: {total_time:.3f}s, results: {len(results)} rows, total_count: {total_count}",
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
    if context_obj and context_obj.unique_id_names:
        unique_id_names = context_obj.unique_id_names or []
        unique_id_names_set = set(unique_id_names)

        # 1. Extract parent IDs from entries/params and validate nesting rules
        all_parent_ids = []
        for i in range(total_logs):
            current_entries = entries_list[min(i, len(entries_list) - 1)] or {}
            current_params = params_list[min(i, len(params_list) - 1)] or {}

            # Merge entries and params to check for unique ID columns
            current_data = {**current_entries, **current_params}

            # Extract parent IDs that match unique ID column names
            parent_ids = {}
            for key in list(current_data.keys()):
                if key in unique_id_names_set:
                    parent_ids[key] = current_data[key]

            # Validate parent IDs follow proper nesting rules
            if parent_ids:
                # Cannot provide the rightmost (auto-incremented) column
                rightmost_col = unique_id_names[-1]
                if rightmost_col in parent_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot provide value for rightmost unique ID column '{rightmost_col}'. This column is auto-incremented.",
                    )

                # Cannot skip hierarchy levels - must provide consecutive columns from left
                provided_indices = [
                    unique_id_names.index(key) for key in parent_ids.keys()
                ]
                if provided_indices:
                    provided_indices.sort()
                    # Check if indices are consecutive starting from 0
                    expected_indices = list(range(len(provided_indices)))
                    if provided_indices != expected_indices:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Parent IDs must be provided for consecutive columns starting from the leftmost. "
                            f"Expected columns: {unique_id_names[:len(provided_indices)]}, "
                            f"but got: {list(parent_ids.keys())}",
                        )

                # Validate that parent keys are valid unique ID names (redundant check but kept for safety)
                for key in parent_ids:
                    if key not in unique_id_names_set:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid parent ID key '{key}'. Allowed keys are: {unique_id_names[:-1]}",
                        )

            all_parent_ids.append(parent_ids)

        # 2. Pop parent ID keys from original entries/params to prevent them from becoming log fields
        for i in range(total_logs):
            current_entries = entries_list[min(i, len(entries_list) - 1)]
            current_params = params_list[min(i, len(params_list) - 1)]
            parent_ids = all_parent_ids[i]

            # Remove parent ID keys from entries and params
            if current_entries:
                for key in parent_ids:
                    current_entries.pop(key, None)
            if current_params:
                for key in parent_ids:
                    current_params.pop(key, None)

        # 3. Construct the `provided_unique_ids` list for the DAO
        provided_unique_ids = all_parent_ids

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

    # Build row_ids payload
    row_ids_payload = None
    unique_id_names = context_obj.unique_id_names or []

    ids_list = []
    # Always return nested format: transform row_ids into a list of lists
    if row_ids and unique_id_names:
        if isinstance(row_ids[0], dict):
            # Nested ID case: transform the list of dictionaries into a list of lists,
            # ensuring the order of values matches the order of column names.
            for id_dict in row_ids:
                ids_list.append([id_dict.get(name) for name in unique_id_names])
        else:
            # Single ID case: wrap each ID in a list to create nested format
            ids_list = [[row_id] for row_id in row_ids]

    row_ids_payload = {
        "names": unique_id_names,
        "ids": ids_list,
    }
    return {"log_event_ids": log_event_ids, "row_ids": row_ids_payload}


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
    start_time = time.time()
    logging.debug(
        f"[_build_unified_logs_subquery] Starting - event_ids: {event_ids is not None}, relevant_log_events: {relevant_log_events is not None}, key: {key}",
    )

    if event_ids is None and relevant_log_events is None:
        raise ValueError("Either event_ids or relevant_log_events must be provided")

    def _apply_event_filter(query, table):
        if event_ids is not None:
            return query.filter(LogEvent.id.in_(event_ids))
        query = query.join(relevant_log_events, relevant_log_events.c.id == LogEvent.id)
        if hasattr(relevant_log_events.c, "row_num"):
            query = query.order_by(relevant_log_events.c.row_num)
        if key:
            return query.filter(table.key == key)
        return query

    # get only the latest version of the logs
    base_logs_q = session.query(
        Log.id.label("id"),
        Log.log_event_id.label("log_event_id"),
        Log.key.label("key"),
        Log.value.label("value"),
        Log.inferred_type.label("inferred_type"),
        Log.param_version.label("param_version"),
        cast(None, Integer).label("context_version"),
        Log.updated_at.label("updated_at"),
        LogEvent.created_at.label("created_at"),
        literal("base").label("source_type"),
    ).join(LogEvent, LogEvent.id == Log.log_event_id)
    base_logs_q = _apply_event_filter(base_logs_q, Log)

    derived_logs_q = session.query(
        DerivedLog.id.label("id"),
        DerivedLog.log_event_id.label("log_event_id"),
        DerivedLog.key.label("key"),
        DerivedLog.value.label("value"),
        DerivedLog.inferred_type.label("inferred_type"),
        # derived logs have no version => cast to None
        cast(None, Integer).label("param_version"),
        cast(None, Integer).label("context_version"),
        DerivedLog.updated_at.label("updated_at"),
        DerivedLog.created_at.label("created_at"),
        literal("derived").label("source_type"),
    ).join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
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

    logging.debug(
        f"[_build_unified_logs_subquery] Completed in {time.time() - start_time:.3f}s",
    )
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
    start_time = time.time()
    logging.debug(
        f"[_format_flat_logs] Starting - {len(rows)} rows, value_limit: {value_limit}, context_len: {context_len}",
    )
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

    logging.debug(
        f"[_format_flat_logs] Completed in {time.time() - start_time:.3f}s - formatted {len(logs_out)} logs",
    )
    return logs_out, params_out


def _get_final_logs(session, filtered_logs_subq, paginated_ids_subq):
    """
    Return fully-hydrated rows, using LATERAL sub-queries so the JSON
    side-tables are probed with indexes instead of being full-scanned.
    """
    start_time = time.time()
    logging.info(
        f"[_get_final_logs] Starting final logs retrieval with LATERAL subqueries",
    )

    # ── current JSON value ────────────────────────────────────────────────
    lateral_setup_start = time.time()
    logging.info(
        f"[_get_final_logs] Building LATERAL subqueries for JSON value retrieval",
    )

    jl_lateral = (
        select(JSONLog.value.label("jl_val"))
        .where(
            and_(
                JSONLog.log_event_id == filtered_logs_subq.c.log_event_id,
                JSONLog.key == filtered_logs_subq.c.key,
            ),
        )
        .limit(1)  # only one row exists anyway
        .lateral()  # turn SELECT into a LATERAL
        .alias("jl_lateral")
    )
    logging.info(f"[_get_final_logs] JSONLog LATERAL subquery created")

    # ── latest history value ──────────────────────────────────────────────
    jlh_lateral = (
        select(JSONLogHistory.value.label("jlh_val"))
        .where(
            and_(
                JSONLogHistory.log_event_id == filtered_logs_subq.c.log_event_id,
                JSONLogHistory.key == filtered_logs_subq.c.key,
            ),
        )
        .order_by(JSONLogHistory.version.desc())
        .limit(1)
        .lateral()
        .alias("jlh_lateral")
    )
    logging.info(f"[_get_final_logs] JSONLogHistory LATERAL subquery created")
    logging.info(
        f"[_get_final_logs] LATERAL subqueries setup completed in {time.time() - lateral_setup_start:.3f}s",
    )

    # -- Main query --------------------------------------------------------------
    main_query_start = time.time()
    logging.info(f"[_get_final_logs] Building main query with coalesce logic and joins")

    final_logs_query = (
        session.query(
            filtered_logs_subq.c.id,
            filtered_logs_subq.c.log_event_id,
            filtered_logs_subq.c.key,
            func.coalesce(
                case(
                    (
                        filtered_logs_subq.c.source_type == "history",
                        jlh_lateral.c.jlh_val,
                    ),
                    else_=jl_lateral.c.jl_val,
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
        # probe the side tables
        .outerjoin(jl_lateral, true())
        .outerjoin(jlh_lateral, true())
        .order_by(paginated_ids_subq.c.row_num, filtered_logs_subq.c.created_at)
    )
    logging.info(
        f"[_get_final_logs] Main query built in {time.time() - main_query_start:.3f}s",
    )

    # Execute the query
    execution_start = time.time()
    logging.info(
        f"[_get_final_logs] Executing final query with LATERAL joins: {final_logs_query}",
    )

    # from sqlalchemy import text
    # try:
    #     import json

    #     # Execute EXPLAIN ANALYZE with the same parameters
    #     compiled_sql = final_logs_query.statement.compile(
    #         dialect=session.bind.dialect,
    #         compile_kwargs={"literal_binds": True},
    #     ).string
    #     compiled_sql = (
    #         "EXPLAIN (ANALYZE, BUFFERS, TIMING, COSTS, VERBOSE, FORMAT JSON) "
    #         + compiled_sql
    #     )
    #     explain_query = text(compiled_sql)
    #     explain_result = session.execute(explain_query)
    #     explain_output = explain_result.fetchone()[0]
    #     with open("explain_analyze.json", "w") as f:
    #         f.write(compiled_sql + "\n")
    #         f.write(json.dumps(explain_output, indent=4))
    #         print("Explain analyze written to explain_analyze.json")
    # except Exception as explain_error:
    #     print(f"Error getting explain analyze: {explain_error}")

    result = final_logs_query.all()
    execution_time = time.time() - execution_start

    total_time = time.time() - start_time
    logging.info(
        f"[_get_final_logs] Query executed in {execution_time:.3f}s - retrieved {len(result)} final log rows",
    )
    logging.info(
        f"[_get_final_logs] Complete final logs retrieval finished in {total_time:.3f}s",
    )

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

    # For each key, add a lateral subquery that gets its value
    for key in log_keys:
        # Create a subquery that gets the value for this key
        key_subq = (
            session.query(Log.value)
            .filter(Log.log_event_id == LogEvent.id, Log.key == key)
            .limit(1)
            .scalar_subquery()
            .label(key)
        )
        base_query = base_query.add_columns(key_subq)

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
    columns: Optional[Dict[str, str]] = None,
    fields_a: Optional[Dict[str, Any]] = None,
    fields_b: Optional[Dict[str, Any]] = None,
    session=None,
):
    """
    Constructs a join query between two subqueries based on the specified join mode.

    Args:
        subq_a: First subquery (aliased as 'A')
        subq_b: Second subquery (aliased as 'B')
        join_expr: SQL expression for the join condition
        mode: Type of join ('inner', 'left', 'right', or 'outer')
        columns: Optional dictionary mapping source columns to new column names

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
    if columns:
        for source_col, new_alias in columns.items():
            if "." not in source_col:
                raise ValueError(
                    f"Column '{source_col}' must be prefixed with table alias 'A.' or 'B.'",
                )

            table_alias, actual_col = source_col.split(".", 1)
            if table_alias.upper() == "A":
                if hasattr(subq_a.c, actual_col):
                    select_columns.append(
                        getattr(subq_a.c, actual_col).label(new_alias),
                    )
                else:
                    raise ValueError(f"Column '{actual_col}' not found in source A")
            elif table_alias.upper() == "B":
                if hasattr(subq_b.c, actual_col):
                    select_columns.append(
                        getattr(subq_b.c, actual_col).label(new_alias),
                    )
                else:
                    raise ValueError(f"Column '{actual_col}' not found in source B")
            else:
                raise ValueError(
                    f"Invalid table alias '{table_alias}' in column '{source_col}'",
                )
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
) -> List[int]:
    """
    Creates new log entries from joined query results.

    Args:
        result_rows: Result rows from the join query
        project_id: ID of the project
        context_id: ID of the context
        field_type_dao: FieldTypeDAO instance for field type operations
        context_dao: ContextDAO instance for context operations
        session: SQLAlchemy session

    Returns:
        List of IDs of the newly created log events
    """
    new_log_ids = []
    now = datetime.now(timezone.utc)

    # Get the context object to check if it's versioned
    context_obj = session.get(Context, context_id)

    # Get existing field types for the project/context
    field_types = field_type_dao.get_field_types(
        project_id,
        return_mutable=True,
        context_id=context_id,
    )

    # Prepare collections for bulk operations
    new_field_types = []
    log_events = []
    log_event_contexts = []
    logs = []
    json_logs = []

    # Process each row
    for row in result_rows:
        # Convert row to dictionary
        row_dict = {}
        for col in row._fields:
            value = getattr(row, col)
            if col != "id":  # Skip the id column as it's special
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
        row_dict = {}
        for col in row._fields:
            value = getattr(row, col)
            if col != "id":  # Skip the id column as it's special
                row_dict[col] = value

        # Create LogEventContext association
        log_event_contexts.append(
            LogEventContext(
                log_event_id=log_event.id,
                context_id=context_id,
            ),
        )

        # Create individual Log entries for each column in the joined result
        for col, val in row_dict.items():
            # Check if field type exists, create if not
            if col not in field_types:
                # Determine if mutable based on versioned context
                mutable = context_obj and context_obj.is_versioned

                # Add to new field types collection
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": col,
                        "value": val,
                        "mutable": mutable,
                        "unique": False,  # Default to non-unique for joined fields
                        "field_category": "entry",  # Joined fields are entries
                        "context_id": context_id,
                    },
                )
            else:
                # Enforce type consistency for existing fields
                field_info = field_types.get(col)
                if field_info:
                    entered_type = LogDAO.infer_type(col, val)
                    expected_type = field_info["field_type"]

                    if expected_type and expected_type != "NoneType":
                        if entered_type != expected_type and entered_type != "NoneType":
                            raise HTTPException(
                                status_code=400,
                                detail=f"Type mismatch for field '{col}' in joined result: expected {expected_type}, got {entered_type}",
                            )

            inferred_type = LogDAO.infer_type(col, val)
            logs.append(
                Log(
                    log_event_id=log_event.id,
                    key=col,
                    value=val,
                    inferred_type=inferred_type,
                    created_at=now,
                    updated_at=now,
                ),
            )

            # If value is a dict or list, create a JSONLog entry
            if isinstance(val, (dict, list)):
                json_logs.append(
                    JSONLog(
                        log_event_id=log_event.id,
                        key=col,
                        value=val,
                    ),
                )

        new_log_ids.append(log_event.id)

    # Bulk create new field types if any
    try:
        if new_field_types:
            field_type_dao.bulk_create_field_types(new_field_types)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Bulk insert related records
    session.bulk_save_objects(log_event_contexts)
    session.bulk_save_objects(logs)
    session.bulk_save_objects(json_logs)

    return new_log_ids


def _join_logs(
    project_id: int,
    project_name: str,
    pair_of_args: List[Dict[str, Any]],
    join_expr: str,
    mode: str,
    context_id: int,
    columns: Optional[Dict[str, str]] = None,
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
        columns: Optional dictionary mapping source columns to new column names.
                 Format should be {'A.column_name': 'new_name', 'B.column_name': 'other_name'}
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
                "Contexts for both queries must be provided in the pair of args. Got: {context_a} and {context_b}",
            )

        filter_expr_a = pair_of_args[0].get("filter_expr")
        filter_expr_b = pair_of_args[1].get("filter_expr")
        if filter_expr_a:
            pair_of_args[0]["filter_expr"] = filter_expr_a.replace(context_a + ".", "")
        if filter_expr_b:
            pair_of_args[1]["filter_expr"] = filter_expr_b.replace(context_b + ".", "")

        # replace context_a with 'A' alias and context_b with 'B' alias
        join_expr = join_expr.replace(context_a, "A").replace(context_b, "B")
        if columns is not None:
            new_columns = {}
            for source_col, new_alias in columns.items():
                processed_source_col = source_col.replace(context_a, "A").replace(
                    context_b,
                    "B",
                )
                new_columns[processed_source_col] = new_alias
            columns = new_columns

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
            session=session,
        )

        # Execute the join query
        result_rows = session.execute(joined_query).fetchall()

        # If no results, return empty list
        if not result_rows:
            return []

        # Create new log entries from the joined results
        new_log_ids = _create_logs_from_joined_rows(
            result_rows=result_rows,
            project_id=project_id,
            context_id=context_id,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )

        # Commit the transaction
        session.commit()

        return new_log_ids

    except Exception as e:
        raise ValueError(f"Failed to join logs: {traceback.format_exc()}")
